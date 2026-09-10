import asyncio
from types import SimpleNamespace

from app.chat.response_plan import ResponsePlan, parse_llm_plan, plan_response, validate_plan


def test_parse_llm_plan_accepts_autonomous_mode():
    plan = parse_llm_plan(
        '{"intent":"state_review","mode":"retrieve_then_answer","confidence":0.91,'
        '"evidence_required":true,"retrieval_required":true,"tool_required":false,'
        '"needs_clarification":false,"risk":"low","constraints":["区分事实和推断"]}'
    )
    assert plan.mode == "retrieve_then_answer"
    assert plan.source == "llm"
    assert plan.retrieval_required is True


def test_low_confidence_falls_back_without_action():
    plan = parse_llm_plan(
        '{"intent":"unknown","mode":"action","confidence":0.2,"tool_required":true,"action":"delete"}'
    )
    assert plan.mode == "casual_chat"
    assert plan.action is None


def test_high_risk_plan_requires_confirmation():
    plan = validate_plan(
        ResponsePlan(mode="action", intent="delete_data", confidence=0.95, risk="high"),
        is_owner=True,
    )
    assert plan.confirmation_required is True


def test_guest_action_is_refused():
    plan = validate_plan(ResponsePlan(mode="action", intent="open_file", confidence=0.9), is_owner=False)
    assert plan.mode == "refuse_or_confirm"
    assert plan.confirmation_required is True


def test_planner_disabled_uses_rule_fallback():
    ctx = SimpleNamespace(message="这个架构有什么问题", is_owner=True, request_id="r1", uid="owner")
    runtime = SimpleNamespace(
        settings=SimpleNamespace(semantic_planner_enabled=False, llm_model="test"),
        llm=SimpleNamespace(chat=None),
    )
    plan = asyncio.run(plan_response(ctx, runtime, []))
    assert plan.source in {"rule", "fallback"}
    assert plan.mode == "reasoning"


def test_planner_enabled_uses_llm_json_result():
    ctx = SimpleNamespace(message="我最近状态怎么样", is_owner=True, request_id="r1", uid="owner")

    class FakeLLM:
        async def chat(self, messages, **kwargs):
            assert kwargs["response_format"] == {"type": "json_object"}
            return '{"intent":"state_review","mode":"retrieve_then_answer","confidence":0.9,"retrieval_required":true}'

    runtime = SimpleNamespace(
        settings=SimpleNamespace(
            semantic_planner_enabled=True,
            semantic_planner_shadow_only=False,
            response_plan_model="",
            response_plan_timeout=12,
            response_plan_max_tokens=400,
            response_plan_min_confidence=0.6,
            llm_model="test",
        ),
        llm=FakeLLM(),
    )
    plan = asyncio.run(plan_response(ctx, runtime, []))
    assert plan.source == "llm"
    assert plan.mode == "retrieve_then_answer"


# ── planner 失败不得丢掉规则层的取证要求 ────────────────────

def _hint_for(message):
    from app.chat.response_plan import build_rule_plan
    return build_rule_plan(message)


def test_unparsable_plan_falls_back_to_rule_plan():
    """planner 输出坏掉时退回规则计划，而不是退成裸的 casual_chat。"""
    hint = _hint_for("最近有什么新闻")
    assert hint.provider == "web_search"

    plan = parse_llm_plan("这不是 JSON", fallback=hint)
    assert plan.provider == "web_search"
    assert plan.intent == "latest_news"
    assert plan.evidence_required is True


def test_fenced_plan_is_parsed():
    plan = parse_llm_plan('```json\n{"mode": "reasoning", "confidence": 0.9}\n```')
    assert plan.mode == "reasoning"
    assert plan.source == "llm"


def test_planner_cannot_cancel_web_search_requirement():
    """规则判定必须联网时，planner 给的 casual_chat 不能取消它。"""
    from app.chat.response_plan import apply_rule_requirements

    hint = _hint_for("湖南四岁幼童事件")
    planned = ResponsePlan(mode="casual_chat", intent="chitchat", confidence=0.9, source="llm")
    merged = apply_rule_requirements(planned, hint)

    assert merged.provider == "web_search"
    assert merged.mode == "retrieve_then_answer"
    assert merged.evidence_required is True
    assert any("未查到" in item for item in merged.constraints)


def test_planner_cannot_cancel_moral_requirements():
    from app.chat.response_plan import apply_rule_requirements

    hint = _hint_for("这件事谁的错")
    planned = ResponsePlan(mode="casual_chat", intent="chitchat", confidence=0.9, source="llm")
    merged = apply_rule_requirements(planned, hint)

    assert merged.needs_moral_judgment is True
    assert merged.evidence_required is True
    assert len(merged.constraints) >= 2


def test_rule_requirements_do_not_override_planner_mode_when_compatible():
    """planner 选了更合适的模式（且不冲突）时保留它的判断。"""
    from app.chat.response_plan import apply_rule_requirements

    hint = _hint_for("最近有什么新闻")
    planned = ResponsePlan(mode="reasoning", intent="analysis", confidence=0.9, source="llm")
    merged = apply_rule_requirements(planned, hint)

    assert merged.mode == "reasoning"
    assert merged.provider == "web_search"
