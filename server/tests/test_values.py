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


def test_clear_cut_detection():
    assert values.is_clear_cut("湖南四岁幼童事件谁的错") is True
    assert values.is_clear_cut("该选哪个技术方案") is False


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
