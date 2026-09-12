"""群聊轻量审校与话题语气测试。"""
import asyncio
import json
from types import SimpleNamespace

from app.chat import prompting, review


def _ctx(message="这个呢？", *, plan=None):
    ctx = SimpleNamespace(
        message=message,
        is_group=True,
        is_owner=False,
        uid="10086",
        request_id="req-group-review",
        trace=SimpleNamespace(response_plan=plan or {}),
    )
    return ctx


def _bundle(history=None):
    return SimpleNamespace(history=history or [], profile="", evidence={})


def test_group_review_skips_trivial_without_context():
    assert review.should_reflect_group(_ctx("你好"), _bundle(), "你好呀。") == []


def test_group_review_triggers_for_ambiguous_followup():
    reasons = review.should_reflect_group(
        _ctx("那另一本呢？"),
        _bundle([{"role": "assistant", "content": "刚才推荐了《基地》。"}]),
        "可以看看《三体》。",
    )
    assert "followup_context" in reasons


def test_group_review_triggers_for_explicit_topic_switch():
    reasons = review.should_reflect_group(
        _ctx("换个话题，晚饭吃什么？"),
        _bundle([{"role": "assistant", "content": "刚才在聊服务器。"}]),
        "可以吃盖饭、汤面或者沙拉。",
    )
    assert "topic_switch" in reasons


def test_group_review_triggers_for_identity_safety():
    reasons = review.should_reflect_group(
        _ctx("你到底是谁的机器人？"), _bundle(), "我是管理员的私人助手。"
    )
    assert "identity_safety" in reasons


def test_group_review_parser_accepts_revision():
    result = review.parse_group_review_result(json.dumps({
        "needs_revision": True,
        "reasons": ["旧话题带偏"],
        "scores": {"relevance": 0.2, "context_fit": 0.3, "safety": 1.0, "tone": 0.8},
        "confidence": 0.9,
        "revised_reply": "换个话题也可以。晚饭想吃清淡点还是满足点？",
    }, ensure_ascii=False))
    assert result.status == "passed"
    assert result.needs_revision is True
    assert "晚饭" in result.revised_reply
    assert result.scores["relevance"] == 0.2


def test_group_review_parser_failure_keeps_safe_status():
    result = review.parse_group_review_result("not json")
    assert result.status == "failed"
    assert result.needs_revision is False
    assert result.revised_reply == ""


def test_group_review_calls_llm_once_and_returns_revision(monkeypatch):
    calls = []

    class _Settings:
        reflection_review_model = ""
        llm_model = "review-model"
        group_reflection_review_timeout = 2.0
        group_reflection_max_tokens = 700

    class _LLM:
        async def chat(self, messages, **kwargs):
            calls.append((messages, kwargs))
            return json.dumps({
                "needs_revision": True,
                "reasons": ["回答被上一话题带偏"],
                "scores": {"relevance": 0.2, "context_fit": 0.2, "safety": 1.0, "tone": 0.8},
                "confidence": 0.9,
                "revised_reply": "换个话题也可以，晚饭想吃什么？",
            }, ensure_ascii=False)

    runtime = SimpleNamespace(settings=_Settings(), llm=_LLM())
    checked, elapsed = asyncio.run(
        review.review_group_reply(
            _ctx("换个话题，晚饭吃什么？"),
            runtime,
            _bundle([{"role": "assistant", "content": "刚才在聊天文。"}]),
            "那继续聊望远镜吧。",
        )
    )
    assert checked.status == "passed"
    assert checked.needs_revision is True
    assert "晚饭" in checked.revised_reply
    assert elapsed >= 0
    assert len(calls) == 1
    payload = calls[0][0][1]["content"]
    assert "天文" in payload and "望远镜" in payload
    assert calls[0][1]["purpose"] == "group_review"


def test_group_prompt_describes_persistent_group_history():
    text = prompting.build_system_prompt(
        _ctx("那另一本呢？"),
        SimpleNamespace(settings=SimpleNamespace(values_enabled=False), logger=None),
        SimpleNamespace(
            injections="[记忆] 群里推荐过《基地》",
            profile="[topics] 科幻",
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
            history=[{"role": "assistant", "content": "刚才推荐了《基地》。"}],
            extra_blocks=[],
        ),
    )
    assert "长期保存并可检索" in text
    assert "群消息不会被长期保存" not in text
    assert "明确换题" in text
    assert "群聊语气" in text


def test_group_tone_changes_by_topic():
    assert "先承接情绪" in prompting.group_tone_hint(_ctx("今天好累，脑子都麻了"))
    assert "先给行动步骤" in prompting.group_tone_hint(_ctx("接口报错了，怎么修？"))
    assert "准确、清楚" in prompting.group_tone_hint(
        _ctx("数据库索引为什么影响查询性能？", plan={"mode": "reasoning"})
    )
    assert "中性克制" in prompting.group_tone_hint(_ctx("你是谁的机器人？"))


def test_group_review_input_excludes_private_profile():
    messages = review.build_group_review_messages(
        _ctx("继续说"),
        _bundle([{"role": "assistant", "content": "群里刚聊到服务器。"}]),
        "可以继续看端口配置。",
    )
    joined = "\n".join(item["content"] for item in messages)
    assert "profile" not in joined.lower()
    assert "服务器" in joined
