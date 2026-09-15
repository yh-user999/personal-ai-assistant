"""群聊社交判断：动作解析、场景摘要与安全门禁。"""
import json
from types import SimpleNamespace

from app.chat import prompting, response_plan
from app.chat.context import ChatContext, ChatRequest
from app.group import context as group_context


def _ctx(message="你好", *, directed=True):
    return ChatContext(
        request=type("Request", (), {"state": type("State", (), {})()})(),
        request_model=ChatRequest(
            message=message,
            group_id="social-test-group",
            group_directed=directed,
        ),
        message=message,
        uid="social-test-user",
        is_owner=False,
        group_id="social-test-group",
        group_directed=directed,
    )


def test_group_social_hint_detects_action_and_atmosphere():
    hint = response_plan.build_group_social_hint("哈哈，怎么选？", addressed=True)
    assert hint["action"] == "ask_back"
    assert hint["atmosphere"] in {"casual", "questioning"}
    assert "question_signal" in hint["reasons"]


def test_directed_social_gate_never_silences_message():
    plan = response_plan.ResponsePlan(social_action="ignore", social_confidence=1.0)
    result = response_plan.apply_group_social_requirements(
        plan,
        message="@小月 你好",
        addressed=True,
    )
    assert result.social_action == "answer"
    assert result.social_addressed is True
    assert "directed_must_answer" in result.social_reasons


def test_non_directed_social_gate_defaults_to_ignore():
    plan = response_plan.ResponsePlan(social_action="interject", social_confidence=0.99)
    result = response_plan.apply_group_social_requirements(
        plan,
        message="大家觉得这个方案怎么样？",
        addressed=False,
        interject_enabled=False,
    )
    assert result.social_action == "ignore"
    assert result.social_confidence == 1.0
    assert "interject_disabled" in result.social_reasons


def test_non_directed_interjection_requires_explicit_enablement():
    hint = response_plan.build_group_social_hint(
        "大家觉得这个方案怎么样？",
        addressed=False,
        interject_enabled=True,
    )
    assert hint["action"] == "interject"
    assert hint["confidence"] < 0.6


def test_planner_payload_contains_bounded_group_scene():
    messages = response_plan.build_planner_messages(
        "换个话题，晚饭吃什么？",
        [{"role": "user", "content": "刚才在聊服务器"}],
        response_plan.ResponsePlan(social_action="answer"),
        is_group=True,
        group_directed=True,
        group_scene={"atmosphere": "casual", "topic_shift": True},
    )
    payload = json.loads(messages[1]["content"])
    assert payload["group_social"]["directed"] is True
    assert payload["group_social"]["scene"] == {"atmosphere": "casual", "topic_shift": True}
    assert "social_action" in messages[0]["content"]


def test_group_scene_summary_is_bounded_and_isolated():
    group_context.clear()
    group_context.remember("social-group-a", "user", "接口报错了，服务器怎么修？", user_id="member-a", now=1000)
    group_context.remember("social-group-a", "assistant", "先看日志和端口。", now=1001)
    group_context.remember("social-group-b", "user", "哈哈今天真开心", user_id="member-b", now=1000)

    scene = group_context.scene_summary("social-group-a", now=1001)
    other = group_context.scene_summary("social-group-b", now=1001)

    assert scene["message_count"] == 2
    assert scene["speaker_count"] == 1
    assert scene["has_recent_bot_reply"] is True
    assert scene["atmosphere"] == "technical"
    assert other["atmosphere"] == "celebration"
    assert group_context.scene_summary("social-group-a", now=1001 + group_context.TTL_SECONDS + 1) == {}
    group_context.clear()


def test_group_prompt_renders_heartflow_expression_rules():
    ctx = _ctx("你又来了，哈哈")
    ctx.trace.response_plan = {"social_action": "answer"}
    bundle = SimpleNamespace(
        injections="", profile="", facts="", lessons="", concerns="", jargon="",
        style_examples="", behavior="", goals_text="", open_issues="",
        knowledge_text="", intent_label="", slang="", mood="", mood_state="",
        self_state="", older=[], history=[], extra_blocks=[],
        robot_state=(
            "【群聊自然表达】短暂联想后立刻回到当前问题。"
            "不要凭空补充事实。"
        ),
    )
    runtime = SimpleNamespace(settings=SimpleNamespace(values_enabled=False), logger=None)

    text = prompting.build_system_prompt(ctx, runtime, bundle)

    assert "群聊自然表达" in text
    assert "不要凭空补充事实" in text


def test_group_prompt_renders_social_action_boundaries():
    ctx = _ctx("你又来了，哈哈")
    ctx.trace.response_plan = {
        "social_action": "tease",
        "social_atmosphere": "casual",
        "social_topic_shift": False,
    }
    bundle = SimpleNamespace(
        injections="",
        profile="",
        facts="",
        lessons="",
        concerns="",
        jargon="",
        style_examples="",
        behavior="",
        goals_text="",
        open_issues="",
        knowledge_text="",
        intent_label="",
        slang="",
        mood="",
        mood_state="",
        self_state="",
        older=[],
        history=[],
        extra_blocks=[],
    )
    runtime = SimpleNamespace(settings=SimpleNamespace(values_enabled=False), logger=None)

    text = prompting.build_system_prompt(ctx, runtime, bundle)

    assert "本轮群聊社交动作" in text
    assert "轻微吐槽当前话题或行为" in text
    assert "不得攻击外貌、疾病、家庭" in text


def test_group_prompt_does_not_inject_private_profile_claims(monkeypatch):
    from app.services import profile as profile_service

    monkeypatch.setattr(
        profile_service,
        "get_profile_injection",
        lambda **kwargs: "[project_info] 实验室",
    )
    ctx = _ctx("今天有点累", directed=True)
    ctx.trace.response_plan = {
        "social_action": "answer",
        "social_reasons": [],
        "social_atmosphere": "emotional",
        "social_topic_shift": False,
    }
    bundle = SimpleNamespace(
        injections="", profile="[project_info] 实验室", facts="", lessons="", concerns="", jargon="",
        style_examples="", behavior="", goals_text="", open_issues="",
        knowledge_text="", intent_label="", slang="", mood="", mood_state="",
        self_state="", older=[], history=[], extra_blocks=[],
    )
    runtime = SimpleNamespace(settings=SimpleNamespace(values_enabled=False), logger=None)

    text = prompting.build_system_prompt(ctx, runtime, bundle)

    assert "实验室" not in text
    assert "未在当前消息或本群上下文确认的个人背景" in text


def test_group_prompt_renders_one_line_care_followup_boundary():
    ctx = _ctx("最近压力很大", directed=True)
    ctx.trace.response_plan = {
        "social_action": "answer",
        "social_reasons": ["emotion_signal", "care_followup"],
        "social_atmosphere": "emotional",
        "social_topic_shift": False,
    }
    bundle = SimpleNamespace(
        injections="", profile="", facts="", lessons="", concerns="", jargon="",
        style_examples="", behavior="", goals_text="", open_issues="",
        knowledge_text="", intent_label="", slang="", mood="", mood_state="",
        self_state="", older=[], history=[], extra_blocks=[],
    )
    runtime = SimpleNamespace(settings=SimpleNamespace(values_enabled=False), logger=None)

    text = prompting.build_system_prompt(ctx, runtime, bundle)

    assert "只用一句自然" in text
    assert "这是一次有限跟进" in text
    assert "不要追问隐私" in text
