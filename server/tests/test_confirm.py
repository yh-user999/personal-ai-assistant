"""指令确认层测试：破坏性/低置信度指令先问再执行。

确认层是"正则治不了自然语言歧义"的兜底：形态闸门挡不掉的歧义
（6 字中文短语与真别名形态相同）交给用户一句确认。
"""
import asyncio
import os

import pytest

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

from app.api import chat as chat_api
from app.config import settings
from app.models.database import connect
from app.services import confirm


class _FakeState:
    collector_heartbeat = None


class _FakeApp:
    state = _FakeState()


class _FakeRequest:
    app = _FakeApp()


@pytest.fixture(autouse=True)
def clean(db, monkeypatch):
    """每个用例独立库 + 清空待确认状态（内存态会跨用例泄漏）。"""
    monkeypatch.setattr(settings, "executor_allowed_roots", "/tmp/allowed")
    confirm.reset()
    yield
    confirm.reset()


def _run(msg, uid="owner"):
    """走命令路由，返回 (命中的 handler 名, 回复文本)。"""
    ctx = {"uid": uid, "is_owner": True}

    async def go():
        for name, handler in chat_api._COMMAND_HANDLERS:
            resp = await handler(msg, _FakeRequest(), ctx)
            if resp is not None:
                return name, resp.reply
        return None, None

    return asyncio.run(go())


def _pending_count():
    conn = connect()
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM executor_commands").fetchone()["n"]
    finally:
        conn.close()


# ── 破坏性操作要确认 ──────────────────────────────────────

def test_move_asks_before_executing():
    name, reply = _run("把/tmp/allowed/a.txt移动到/tmp/allowed/old/")
    assert "需要我" in reply and "确认" in reply
    assert _pending_count() == 0, "确认前不该入队"


def test_confirm_then_enqueues():
    _run("把/tmp/allowed/a.txt移动到/tmp/allowed/old/")
    name, reply = _run("确认")
    assert name == "confirm"
    assert "已收到指令" in reply
    assert _pending_count() == 1


def test_cancel_discards():
    _run("把/tmp/allowed/a.txt移动到/tmp/allowed/old/")
    name, reply = _run("取消")
    assert name == "confirm"
    assert "已取消" in reply
    assert _pending_count() == 0


@pytest.mark.parametrize("word", ["确认", "确定", "是", "好", "可以", "ok", "yes", "嗯"])
def test_confirm_synonyms(word):
    _run("把/tmp/allowed/a.txt移动到/tmp/allowed/old/")
    _, reply = _run(word)
    assert "已收到指令" in reply, word


@pytest.mark.parametrize("word", ["取消", "不用", "算了", "别", "no"])
def test_cancel_synonyms(word):
    _run("把/tmp/allowed/a.txt移动到/tmp/allowed/old/")
    _, reply = _run(word)
    assert "已取消" in reply, word


def test_unrelated_reply_drops_pending_and_falls_through():
    """用户没回答而是说了别的：放弃挂起指令，消息正常往下走。

    否则"确认"状态会黏住，用户下一句无论说什么都被当成对上一条的回答。
    """
    _run("把/tmp/allowed/a.txt移动到/tmp/allowed/old/")
    name, reply = _run("算了我们聊点别的吧")
    assert confirm.peek("owner") is None, "挂起指令应被丢弃"
    assert _pending_count() == 0


def test_read_operations_not_gated():
    """读类操作不打扰用户（list_dir/read_file 无破坏性）。"""
    for msg in ["看看/tmp/allowed目录有什么", "读一下 /tmp/allowed/a.txt"]:
        confirm.reset()
        name, reply = _run(msg)
        assert "已收到指令" in reply, msg


def test_explicit_path_open_not_gated():
    """给了明确路径的 open 是高置信度，直接执行。"""
    name, reply = _run("打开 /tmp/allowed")
    assert "已收到指令" in reply


def test_ambiguous_open_is_gated():
    """形态像别名但无法核实的长中文短语 → 先问。"""
    name, reply = _run("打开新世界的大门")
    assert "需要我" in reply
    assert _pending_count() == 0


def test_timeout_expires_pending(monkeypatch):
    confirm.remember("owner", "move", "x", "移动 x")
    monkeypatch.setattr(confirm, "PENDING_TTL_SECONDS", -1)
    assert confirm.take("owner") is None


def test_pending_capped(monkeypatch):
    """防伪造 uid 撑爆内存。"""
    monkeypatch.setattr(confirm, "MAX_PENDING", 5)
    for i in range(20):
        confirm.remember(f"user{i}", "move", "x", "d")
    assert len(confirm._pending) <= 5


def test_new_command_replaces_previous_pending():
    confirm.remember("owner", "move", "first", "移动 first")
    confirm.remember("owner", "move", "second", "移动 second")
    item = confirm.take("owner")
    assert item["target"] == "second"


def test_guest_cannot_confirm_owner_command():
    """访客被屏蔽在 confirm handler 之外，不能确认主人挂起的指令。"""
    assert "confirm" in chat_api.GUEST_BLOCKED_HANDLERS


def test_describe_command_is_human_readable():
    from app.services import executor as srv

    desc = srv.describe_command("move", srv._pack("F:/a.txt", "F:/old/"))
    assert "F:/a.txt" in desc and "F:/old/" in desc and "移动" in desc
