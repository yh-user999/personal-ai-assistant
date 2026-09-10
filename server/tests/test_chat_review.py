from types import SimpleNamespace

import pytest

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


# ── 价值基线：道德类硬门槛 ──────────────────────────────────

def _moral_review(**flags):
    base = {"relevance": 0.95, "state_fit": 0.95, "grounding": 0.95,
            "tone": 0.95, "brevity": 0.95, "safety": 1.0,
            "stance_clarity": 0.95, "noise_resistance": 0.95, "proportionality": 0.95}
    return review.ReplyReview(needs_revision=False, scores=base, **flags)


@pytest.mark.parametrize("flag", [
    "noise_used_as_reason",
    "moralizes_unverified",
    "escalates_to_person",
    "substitutes_authority",
    "empty_neutrality",
])
def test_moral_flags_force_revision(flag):
    checked = _moral_review(**{flag: True})
    assert review.should_revise(checked, _ctx("这件事谁的错")) is True


def test_high_scores_alone_do_not_bypass_moral_gate():
    """平均分很高但道德判定命中时仍必须重写（硬门槛不被均分掩盖）。"""
    checked = _moral_review(noise_used_as_reason=True)
    assert checked.quality > 0.9
    assert review.should_revise(checked, _ctx("这件事谁的错")) is True


def test_clean_moral_review_passes():
    checked = _moral_review()
    assert review.should_revise(checked, _ctx("这件事谁的错")) is False


def test_parse_reads_moral_flags_and_new_scores():
    result = review.parse_review_result(
        '{"needs_revision":false,"scores":{"stance_clarity":0.8,"noise_resistance":0.7,'
        '"proportionality":0.6},"noise_used_as_reason":true,"empty_neutrality":false,'
        '"issues":[],"revision_plan":[],"confidence":0.9}'
    )
    assert result.noise_used_as_reason is True
    assert result.empty_neutrality is False
    assert result.scores["stance_clarity"] == 0.8
    assert "noise_used_as_reason" in result.moral_gates


def test_parse_defaults_moral_flags_to_false():
    result = review.parse_review_result('{"needs_revision":false,"scores":{}}')
    assert result.moral_gates == []


# ── 确定性兜底：审校器漏判也能拦住 ──────────────────────────

def test_deterministic_gate_catches_person_attack():
    checked = _moral_review()
    assert review.should_revise(checked, _ctx("怎么评价"), draft="这种人就是垃圾") is True


def test_deterministic_gate_catches_authority_substitute():
    checked = _moral_review()
    assert review.should_revise(checked, _ctx("怎么评价"), draft="他就是故意犯罪") is True


def test_deterministic_gate_catches_noise_marker():
    checked = _moral_review()
    assert review.should_revise(checked, _ctx("怎么评价"), draft="网上都说他不对") is True


def test_deterministic_gate_catches_empty_neutrality_on_clear_cut():
    checked = _moral_review()
    assert review.should_revise(
        checked, _ctx("湖南幼童事件谁的错"), draft="双方都有道理"
    ) is True


def test_empty_neutrality_allowed_when_topic_is_contested_not_clear_cut():
    """非明确是非的议题，保留分歧不算空洞中立。"""
    checked = _moral_review()
    assert review.should_revise(
        checked, _ctx("这两个技术方案该怎么选"), draft="各有各的道理"
    ) is False


# ── 触发闸门 ────────────────────────────────────────────────

def test_short_reply_still_reflects_when_plan_is_moral():
    ctx = _ctx("这件事谁的错")
    ctx.trace.response_plan = {"mode": "moral_assessment", "needs_moral_judgment": True}
    bundle = SimpleNamespace(facts="", lessons="", mood="", mood_state="", behavior="", self_state="")
    assert "moral_claim" in review.should_reflect(ctx, bundle, "不对。")


def test_short_reply_still_reflects_when_noise_present():
    ctx = _ctx("说说看")
    bundle = SimpleNamespace(facts="", lessons="", mood="", mood_state="", behavior="", self_state="")
    assert "public_opinion" in review.should_reflect(ctx, bundle, "网上都说他有错。")


def test_short_plain_reply_still_skips():
    ctx = _ctx("你好")
    bundle = SimpleNamespace(facts="", lessons="", mood="", mood_state="", behavior="", self_state="")
    assert review.should_reflect(ctx, bundle, "你好呀。") == []


def test_review_prompt_requires_moral_judgement_fields():
    ctx = _ctx("这件事谁的错")
    ctx.trace.response_plan = {"mode": "moral_assessment"}
    messages = review.build_review_messages(
        ctx,
        SimpleNamespace(facts="", lessons="", mood="", mood_state="", behavior="", self_state=""),
        "草稿",
    )
    joined = "\n".join(item["content"] for item in messages)
    assert "noise_used_as_reason" in joined
    assert "empty_neutrality" in joined


# ── 审校输出容错：模型常带围栏或思考前缀 ─────────────────────

def test_parse_review_tolerates_fenced_json():
    result = review.parse_review_result(
        '```json\n{"needs_revision": false, "scores": {"safety": 1.0}, "issues": []}\n```'
    )
    assert result.status == "passed"
    assert result.scores["safety"] == 1.0


def test_parse_review_tolerates_preamble():
    result = review.parse_review_result(
        '我先审一下：\n{"needs_revision": true, "scores": {"relevance": 0.3}}\n完毕。'
    )
    assert result.status == "passed"
    assert result.needs_revision is True


def test_truncated_review_marks_failed_and_is_logged(caplog):
    """被 max_tokens 截断时必须是显式 failed，而不是静默通过。"""
    result = review.parse_review_result('{"needs_revision": false, "scores": {"safety": 1.0')
    assert result.status == "failed"
    assert any("无法解析" in record.message for record in caplog.records)
