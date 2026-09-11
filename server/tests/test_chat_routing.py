"""聊天命令路由层测试：注册顺序契约、访客门禁、快捷命令零 LLM。"""
import asyncio
from types import SimpleNamespace

import pytest

from app.chat import routing
from app.chat.context import ChatContext, ChatRequest, ChatResponse

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

def _identity_context(message: str, *, is_owner: bool) -> ChatContext:
    return ChatContext(
        request=type("Request", (), {"state": type("State", (), {})()})(),
        request_model=ChatRequest(message=message),
        message=message,
        uid="owner" if is_owner else "guest-test",
        is_owner=is_owner,
    )


def test_admin_identity_query_is_detected_without_matching_identity_settings():
    assert routing.is_admin_identity_query("你的管理员是谁")
    assert routing.is_admin_identity_query("主人有几个")
    assert routing.is_admin_identity_query("某某是不是管理员")
    assert routing.is_admin_identity_query("管理员的 QQ 号是多少")
    assert not routing.is_admin_identity_query("你叫什么名字")
    assert not routing.is_admin_identity_query("你是不是 AI")


def test_guest_admin_identity_query_never_confirms_or_denies():
    reply = asyncio.run(
        routing._identity(
            _identity_context("你的管理员是谁", is_owner=False),
            SimpleNamespace(),
        )
    )
    assert reply is not None and reply.reply == "这个我不透露。"
    assert "管理员" not in reply.reply
    assert "不是" not in reply.reply


def test_dispatch_allows_guest_identity_privacy_handler_before_guest_gate():
    ctx = _identity_context("管理员是谁", is_owner=False)
    result = asyncio.run(routing.dispatch(ctx, SimpleNamespace()))
    assert result is not None and result.reply == "这个我不透露。"
    assert ctx.trace.route_name == "command:identity"


def test_authenticated_owner_can_confirm_own_admin_identity():
    reply = asyncio.run(
        routing._identity(
            _identity_context("我是不是管理员", is_owner=True),
            SimpleNamespace(),
        )
    )
    assert reply is not None and "已通过管理员身份认证" in reply.reply


def test_owner_cannot_query_third_party_admin_identity():
    reply = asyncio.run(
        routing._identity(
            _identity_context("某某是不是管理员", is_owner=True),
            SimpleNamespace(),
        )
    )
    assert reply is not None and reply.reply == "这个我不透露。"


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

    async def run():
        reply = await routing._handle_time(
            "几点了",
            type("Request", (), {"state": type("State", (), {})()})(),
            {"uid": "", "is_owner": True},
        )
        return reply

    reply = pytest.importorskip("asyncio").run(run())
    assert reply is not None and "现在是" in reply.reply


def _fitness_context(message: str, *, request_id: str | None = None) -> ChatContext:
    return ChatContext(
        request=type("Request", (), {"state": type("State", (), {})()})(),
        request_model=ChatRequest(message=message, request_id=request_id),
        message=message,
        uid="owner",
        is_owner=True,
    )


def _fitness_runtime(nutrition):
    return SimpleNamespace(
        services=SimpleNamespace(
            fitness=SimpleNamespace(
                parse_weight=lambda _msg: None,
                parse_training=lambda _msg: None,
                PROGRESS_WORDS=(),
            ),
            fitness_training=None,
            fitness_catalog=None,
            fitness_nutrition=nutrition,
        )
    )


def test_fitness_chat_records_food_with_request_id_idempotency_key():
    calls = {}

    class Nutrition:
        def parse_food_log_command(self, message):
            return {"food_name": "燕麦", "grams": 50} if message.startswith("记录饮食") else None

        def list_foods(self, *, query, limit):
            assert query == "燕麦" and limit == 5
            return [{"id": 7, "name": "燕麦", "calories_kcal": 389}]

        def log_food(self, food_id, *, grams, source, external_id, user_id):
            calls.update(food_id=food_id, grams=grams, source=source, external_id=external_id, user_id=user_id)
            return {"food_name": "燕麦", "grams": grams, "calories_kcal": 194.5}

        def format_food_log_reply(self, food, nutrition):
            return f"{food['name']} {nutrition['grams']}g 已记录"

    reply = asyncio.run(
        routing._fitness(
            _fitness_context("记录饮食：燕麦 50g", request_id="qq-request-1"),
            _fitness_runtime(Nutrition()),
        )
    )
    assert reply is not None and reply.reply == "燕麦 50g 已记录"
    assert calls == {
        "food_id": 7,
        "grams": 50,
        "source": "chat",
        "external_id": "chat:qq-request-1",
        "user_id": "owner",
    }


def test_fitness_chat_returns_nutrition_summary_without_llm():
    class Nutrition:
        NUTRITION_SUMMARY_WORDS = {"今日营养"}

        def parse_food_log_command(self, _message):
            return None

        def nutrition_summary(self, *, user_id):
            assert user_id == "owner"
            return {"date": "2026-09-08", "calories_kcal": 500}

        def list_food_logs(self, *, date, limit, user_id):
            assert (date, limit, user_id) == ("2026-09-08", 10, "owner")
            return []

        def format_nutrition_summary(self, summary, logs):
            assert summary["calories_kcal"] == 500 and logs == []
            return "今日营养汇总"

    reply = asyncio.run(routing._fitness(_fitness_context("今日营养"), _fitness_runtime(Nutrition())))
    assert reply is not None and reply.reply == "今日营养汇总"
