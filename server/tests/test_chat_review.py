from types import SimpleNamespace

from app.chat import review


def _ctx(message="李羽的能力是什么？"):
    return SimpleNamespace(
        message=message,
        is_owner=True,
        uid="owner",
        request_id="req-1",
        trace=SimpleNamespace(trace_id="trace-1", route_name="/api/chat"),
    )


def test_parse_review_result_clamps_scores_and_limits_lists():
    result = review.parse_review_result(
        '{"needs_revision":true,"scores":{"relevance":2,"grounding":"0.6","tone":-1},'
        '"issues":["a","b","c","d","e","f","g","h","i"],'
        '"revision_plan":["fix"],"confidence":1.2}'
    )
    assert result.needs_revision is True
    assert result.scores["relevance"] == 1.0
    assert result.scores["grounding"] == 0.6
    assert result.scores["tone"] == 0.0
    assert len(result.issues) == 8
    assert result.confidence == 1.0


def test_invalid_review_result_falls_back_without_revision():
    result = review.parse_review_result("not json")
    assert result.status == "failed"
    assert result.needs_revision is False
    assert result.issues


def test_simple_chat_skips_reflection():
    ctx = _ctx("你好")
    bundle = SimpleNamespace(facts="", lessons="", mood="", mood_state="", behavior="", self_state="")
    assert review.should_reflect(ctx, bundle, "你好呀。") == []


def test_fact_question_triggers_reflection():
    ctx = _ctx()
    bundle = SimpleNamespace(facts="- 李羽 能力 杀人变强", lessons="", mood="", mood_state="", behavior="", self_state="")
    assert "fact_question" in review.should_reflect(ctx, bundle, "他的能力是杀人变强。")


def test_revision_uses_hard_grounding_gate():
    ctx = _ctx()
    result = review.ReplyReview(
        needs_revision=False,
        scores={"relevance": 0.9, "state_fit": 0.9, "grounding": 0.6, "tone": 0.9, "brevity": 0.9, "safety": 1.0},
    )
    assert review.should_revise(result, ctx) is True


def test_revision_keeps_natural_tone_requirement():
    messages = review.build_review_messages(
        _ctx("我今天有点累"),
        SimpleNamespace(facts="", lessons="", mood="疲惫", mood_state="", behavior="", self_state=""),
        "那你先休息吧。",
    )
    joined = "\n".join(item["content"] for item in messages)
    assert "自然聊天口吻" in joined
    assert "固定开场" in joined
