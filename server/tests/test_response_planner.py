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
