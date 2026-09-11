"""价值基线与抗噪音判据测试（纯函数，不调用 LLM、不读库）。"""
import pytest

from app.chat import values


def test_core_principles_are_stable_and_deduped():
    assert len(values.CORE_PRINCIPLES) >= 8
    assert len(set(values.CORE_PRINCIPLES)) == len(values.CORE_PRINCIPLES)
    assert all(len(item) <= 60 for item in values.CORE_PRINCIPLES)


def test_render_principles_marks_each_item():
    text = values.render_principles()
    assert text.count("- ") == len(values.CORE_PRINCIPLES)


def test_render_contested_lists_all_topics():
    text = values.render_contested()
    for key in values.CONTESTED_GUIDANCE:
        assert key in text


# ── 噪音检测 ────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "很多人说这个方案不行",
    "网上都说他有问题",
    "舆论压力太大所以改口了",
    "热搜上都在骂",
])
def test_scan_noise_hits(text):
    assert values.scan_noise(text)


@pytest.mark.parametrize("text", [
    "这件事的关键在于责任划分",
    "我查到的报道给出了两个版本",
    "先看已经确认的事实",
])
def test_scan_noise_misses_normal_text(text):
    assert values.scan_noise(text) == []


# ── 索要判断（路由判据，必须窄）─────────────────────────────

@pytest.mark.parametrize("text", [
    "你觉得该不该处罚",
    "这件事谁的错",
    "这么处理合理吗",
    "你怎么看这件事",
    "说说你的什么看法",
])
def test_judgment_request_hits(text):
    assert values.looks_like_judgment_request(text) is True


@pytest.mark.parametrize("text", [
    "我应该怎么学 RAG",
    "今天天气怎么样",
    "应该先改哪个文件",
    "最近有什么新闻",
])
def test_judgment_request_does_not_overreach(text):
    assert values.looks_like_judgment_request(text) is False


# ── 敏感主体 ────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("湖南四岁幼童事件", True),
    ("涉及未成年人的报道", True),
    ("老人被打伤", True),
    ("帮我写个函数", False),
])
def test_sensitive_subject(text, expected):
    assert values.is_sensitive_subject(text) is expected


# ── 候选回复侧判据 ──────────────────────────────────────────

def test_moral_claim_detection():
    assert values.looks_like_moral_claim("这么做显然过分了") is True
    assert values.looks_like_moral_claim("各有各的道理吧") is True
    assert values.looks_like_moral_claim("文件已经改好了") is False


def test_person_attack_detection():
    assert values.looks_like_person_attack("这种人就是垃圾") is True
    assert values.looks_like_person_attack("这个做法不妥") is False


def test_authority_substitute_detection():
    assert values.looks_like_authority_substitute("他就是故意犯罪") is True
    assert values.looks_like_authority_substitute("等官方通报更稳妥") is False


def test_empty_neutrality_detection():
    assert values.looks_like_empty_neutrality("双方都有道理") is True
    assert values.looks_like_empty_neutrality("我认为责任在施工方") is False


@pytest.mark.parametrize("text", [
    "湖南四岁幼童事件谁的错", "关于未成年人的报道", "发生暴力和伤害谁的错",
    "为了保护孩子把人拉开，有身体接触就是恶吗", "该选哪个技术方案",
])
def test_risk_words_do_not_establish_clear_cut_facts(text):
    assert values.is_clear_cut(text) is False


def test_layered_constraints_are_complete_bounded_and_independent():
    import json

    constraints = values.judgment_constraints()
    assert constraints["version"] == 1
    assert set(constraints) == {"version", "layers", "fact_axis", "procedure_axis"}
    assert [layer["id"] for layer in constraints["layers"]] == [
        "baseline", "fair_attribution", "understanding_repair",
    ]
    assert [layer["name"] for layer in constraints["layers"]] == ["基本底线", "公平归责", "理解修复"]
    assert tuple(item for layer in constraints["layers"] for item in layer["principles"]) == values.CORE_PRINCIPLES
    assert len(json.dumps(constraints, ensure_ascii=False)) < 2600
    rendered = values.render_principles()
    for required in (
        "不当伤害", "人格尊严", "诚实", "能力不足", "公平回应机会", "主观状态有证据",
        "必要性", "谁升级冲突", "先错不授权过度反应", "同理心不是免责", "身体接触",
        "独立事实轴", "独立程序轴", "不等于责任相等", "supported", "unknown", "同源转载",
    ):
        assert required in rendered
    assert rendered.index("第1层·基本底线") < rendered.index("第2层·公平归责") < rendered.index("第3层·理解修复")
    constraints["layers"][0]["principles"].clear()
    assert values.judgment_constraints()["layers"][0]["principles"]
    assert values.render_principles(2).count("- ") == 2


def _action_evidence():
    quote = "甲将乙从行驶车辆旁拉开，随后松手。"
    return {
        "version": 1,
        "sources": [{"id": "s1", "url": "https://example.test/report", "text": quote}],
        "claims": [{
            "id": "c1", "actor": "甲", "action": "拉开后松手", "target": "乙",
            "status": "supported", "support": [{"source_id": "s1", "quote": quote}], "oppose": [],
        }],
    }


def test_traceable_action_is_not_a_truth_or_morality_verdict():
    evidence = _action_evidence()
    assert values.traceable_action_claim_ids(evidence) == {"c1"}
    assert values.is_clear_cut("甲和乙有身体接触，幼童事件谁的错") is False
    evidence["checks"] = {"source_count": 999, "facts_verified": True}
    evidence["claims"][0]["action"] = ""
    assert values.traceable_action_claim_ids(evidence) == set()


@pytest.mark.parametrize("field", ["actor", "action", "target"])
def test_withdrawing_action_premises_removes_traceable_basis(field):
    evidence = _action_evidence()
    evidence["claims"][0].pop(field)
    assert values.traceable_action_claim_ids(evidence) == set()


@pytest.mark.parametrize("status", ["unknown", "disputed", "verified", None])
def test_claim_status_does_not_invent_support(status):
    evidence = _action_evidence()
    evidence["claims"][0]["status"] = status
    assert values.traceable_action_claim_ids(evidence) == set()


def test_traceable_basis_requires_quote_to_match_identified_source():
    evidence = _action_evidence()
    evidence["claims"][0]["support"][0]["quote"] = "甲对乙实施了不当伤害。"
    assert values.traceable_action_claim_ids(evidence) == set()
    evidence = _action_evidence()
    evidence["sources"].append(dict(evidence["sources"][0]))
    assert values.traceable_action_claim_ids(evidence) == set()


@pytest.mark.parametrize("evidence", [None, {}, [], {"version": "1"}, {"version": 1, "sources": {}, "claims": {}}])
def test_malformed_evidence_is_not_a_basis(evidence):
    assert values.traceable_action_claim_ids(evidence) == set()


# ── 三层判断框架 ────────────────────────────────────────────

def test_judgment_frame_renders_three_layers():
    frame = values.JudgmentFrame(
        confirmed_facts=["事实A"],
        disputed_points=["争议B"],
        my_judgment="判断C",
        as_of="2026-09-10",
    )
    text = frame.render()
    assert "已确认事实：事实A" in text
    assert "说法不一致之处：争议B" in text
    assert "我的判断：判断C" in text
    assert "2026-09-10" in text


def test_judgment_frame_omits_empty_layers():
    text = values.JudgmentFrame(my_judgment="只有判断").render()
    assert "已确认事实" not in text
    assert "我的判断：只有判断" in text


# ── 假平衡 / 程序冒充是非（这次对话暴露的和稀泥形态）──────────

def test_false_balance_detected():
    from app.chat import values as v
    assert v.looks_like_false_balance("网上一边骂家长一边骂女方，两边都是拿情绪当证据")
    assert v.looks_like_false_balance("这事各打五十大板，谁都没错")
    assert v.looks_like_false_balance("双方都有问题")
    # 明确表态不算假平衡
    assert not v.looks_like_false_balance("错在成年人一方，孩子无辜，这没有中间地带")


def test_procedure_as_verdict_detected():
    from app.chat import values as v
    assert v.looks_like_procedure_as_verdict("家长已经道歉赔付，该履行的履行了")
    assert v.looks_like_procedure_as_verdict("都调解完了，责任到位了")
    # 单纯陈述程序、不当结论的，不该命中
    assert not v.looks_like_procedure_as_verdict("调解了三次没谈拢")
