"""流式聊天链路回归：增量回调、失败语义、持久化，及全量路径不受影响。

复用 test_chat_pipeline 的桩集合与 db 夹具，验证 run_chat_stream 与
run_chat 在业务语义（入库、友好错误、长文重试）上保持一致。
"""
import asyncio

from app.chat.pipeline import run_chat, run_chat_stream
from app.models.database import connect

from tests.test_chat_pipeline import db_env as db_env  # noqa: F401  pytest 夹具跨模块复用
from tests.test_chat_pipeline import make_ctx, make_runtime


class _StreamLLM:
    """流式 LLM 桩：脚本化每轮 chat_stream 的产出（文本串/异常混排）。"""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.calls = 0

    async def chat(self, messages, **kwargs):  # pragma: no cover - 守护断言
        raise AssertionError("流式路径不应调用全量 chat()")

    async def chat_stream(self, messages, **kwargs):
        self.calls += 1
        action = self.scripts.pop(0)
        for piece in action:
            if isinstance(piece, BaseException):
                raise piece
            yield piece


def test_stream_yields_deltas_and_persists(db_env):
    llm = _StreamLLM([["你好", "呀"]])
    runtime = make_runtime(llm=llm)
    ctx = make_ctx("打招呼")
    seen = []

    async def on_delta(text):
        seen.append(text)

    resp = asyncio.run(run_chat_stream(ctx, runtime, on_delta))

    assert seen == ["你好", "呀"]
    assert resp.reply == "你好呀"

    conn = connect()
    try:
        senders = {
            row["sender"]
            for row in conn.execute(
                "SELECT sender FROM memories WHERE content IN ('打招呼', '你好呀')"
            ).fetchall()
        }
    finally:
        conn.close()
    assert {"user", "assistant"} <= senders


def test_stream_failure_returns_friendly_reply(db_env):
    llm = _StreamLLM([[RuntimeError("backend down")]])
    runtime = make_runtime(llm=llm)
    ctx = make_ctx("你好")
    seen = []

    async def on_delta(text):
        seen.append(text)

    resp = asyncio.run(run_chat_stream(ctx, runtime, on_delta))

    assert "连不上大脑" in resp.reply
    assert seen == [], "失败时不得有增量下发"

    conn = connect()
    try:
        ai_rows = conn.execute(
            "SELECT id FROM memories WHERE sender='assistant'"
        ).fetchall()
    finally:
        conn.close()
    assert ai_rows == [], "流式失败时 assistant 消息不得入库"


def test_stream_gen_retry_only_before_first_delta(db_env):
    """长文生成：首个增量前失败允许重试一次，成功后全文正常返回。"""
    llm = _StreamLLM([[RuntimeError("timeout")], ["生成的长文"]])
    runtime = make_runtime(llm=llm)
    ctx = make_ctx("继续写第三章")
    seen = []

    async def on_delta(text):
        seen.append(text)

    resp = asyncio.run(run_chat_stream(ctx, runtime, on_delta))

    assert resp.reply == "生成的长文"
    assert seen == ["生成的长文"]
    assert llm.calls == 2


def test_stream_gen_midway_failure_does_not_retry(db_env):
    """已输出增量后失败：不再重试（避免客户端收到重复内容），按失败收场。"""
    llm = _StreamLLM([["部分", RuntimeError("dropped")]])
    runtime = make_runtime(llm=llm)
    ctx = make_ctx("继续写第三章")
    seen = []

    async def on_delta(text):
        seen.append(text)

    resp = asyncio.run(run_chat_stream(ctx, runtime, on_delta))

    assert "长文生成连续两次失败" in resp.reply
    assert seen == ["部分"]
    assert llm.calls == 1


def test_plain_run_chat_still_uses_full_call(db_env):
    """全量路径回归：run_chat 只走 chat()，不触发流式。"""

    class _OnlyChat:
        async def chat(self, messages, **kwargs):
            return "全量回复"

        async def chat_stream(self, *a, **k):  # pragma: no cover - 守护断言
            raise AssertionError("run_chat 不应触发流式调用")

    resp = asyncio.run(run_chat(make_ctx("你好"), make_runtime(llm=_OnlyChat())))
    assert resp.reply == "全量回复"
