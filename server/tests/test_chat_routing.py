"""聊天命令路由层测试：注册顺序契约、访客门禁、快捷命令零 LLM。"""
import pytest

from app.chat.context import ChatContext, ChatRequest, ChatResponse
from app.chat import routing


# ── 注册顺序契约 ───────────────────────────────────────────

def test_command_handler_order_contract():
    names = [name for name, _ in routing._COMMAND_HANDLERS]
    # identity 必须最前（身份变更先于一切解析），confirm 次之，
    # executor 必须在 confirm 之后（确认要抢在执行器前面消费）。
    assert names[0] == "identity"
    assert names[1] == "confirm"
    assert names.index("confirm") < names.index("executor")


def test_guest_blocked_handlers_cover_dangerous_commands():
    for name in ("identity", "confirm", "worklog", "reminders", "documents",
                 "resume", "fitness", "novel", "entity_candidates", "executor"):
        assert name in routing.GUEST_BLOCKED_HANDLERS, name


def test_dispatch_skips_guest_blocked_and_falls_through():
    seen = []

    async def fake_handler(msg, request, ctx, runtime=None):
        seen.append(ctx["uid"])
        return None

    original = routing._COMMAND_HANDLERS
    routing._COMMAND_HANDLERS = [("executor", fake_handler), ("time", fake_handler)]
    try:
        ctx = ChatContext(
            request=type("Request", (), {"state": type("State", (), {})()})(),
            request_model=ChatRequest(message="今天星期几"),
            message="今天星期几",
            uid="10086",
            is_owner=False,
        )
        runtime = type("Runtime", (), {"settings": None})()
        result = pytest.importorskip("asyncio").run(routing.dispatch(ctx, runtime))
        assert result is None
        assert seen == ["10086"], "executor 被访客门禁跳过，time 继续执行"
    finally:
        routing._COMMAND_HANDLERS = original


# ── 快捷时间问答（零 LLM）───────────────────────────────────

def test_parse_time_question_hits_and_misses():
    assert "现在是" in routing.parse_time_question("几点了")
    assert "现在是" in routing.parse_time_question("今天星期几")
    assert routing.parse_time_question("帮我写个周报") is None


# ── dispatch 命中即短路 ────────────────────────────────────

def test_dispatch_short_circuits_on_first_hit():
    calls = []

    async def hit_handler(msg, request, ctx, runtime=None):
        calls.append("hit")
        return ChatResponse(reply="命中", memories_used=0)

    async def never_handler(msg, request, ctx, runtime=None):
        calls.append("never")
        return None

    original = routing._COMMAND_HANDLERS
    routing._COMMAND_HANDLERS = [("time", hit_handler), ("search", never_handler)]
    try:
        ctx = ChatContext(
            request=type("Request", (), {"state": type("State", (), {})()})(),
            request_model=ChatRequest(message="几点了"),
            message="几点了",
            uid="",
            is_owner=True,
        )
        runtime = type("Runtime", (), {"settings": None})()
        result = pytest.importorskip("asyncio").run(routing.dispatch(ctx, runtime))
        assert result is not None and result.reply == "命中"
        assert calls == ["hit"], "命中后不得继续执行后续 handler"
    finally:
        routing._COMMAND_HANDLERS = original


def test_legacy_handler_signature_still_supported():
    """旧 (msg, request, ctx) 调用形态兼容：不传 runtime 也能工作。"""
    from types import SimpleNamespace

    async def run():
        reply = await routing._handle_time(
            "几点了",
            type("Request", (), {"state": type("State", (), {})()})(),
            {"uid": "", "is_owner": True},
        )
        return reply

    reply = pytest.importorskip("asyncio").run(run())
    assert reply is not None and "现在是" in reply.reply
