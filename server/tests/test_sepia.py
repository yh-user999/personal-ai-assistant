"""Sepia 小说生成/审校规则与表层预检测试。"""
import json

from app.services import chapter_analysis as ca
from app.services import sepia


def test_generation_and_review_guides_are_compact_and_have_boundaries():
    generation = sepia.build_generation_block()
    review = sepia.build_review_block()

    assert generation.startswith("【Sepia 小说生成规则】")
    assert "叙事层" in generation and "话语层" in generation
    assert "只输出小说正文" in generation
    assert "权威设定" in generation
    assert "不得输出" in generation
    assert review.startswith("【Sepia 小说审校规则】")
    assert all(term in review for term in ("叙事", "话语", "表层"))
    assert "明确引句" in review
    # 规则模块是压缩后的本地规则，不应把外部技能原文整段带进 prompt。
    assert "StoryScope" not in generation + review
    assert len(generation) < 1800 and len(review) < 1000


def test_detect_surface_violations_covers_legacy_and_sepia_rules():
    text = (
        "第一章 风起\n"
        "本章完\n"
        "写完了。你看看节奏对不对。\n"
        "章节说明：\n"
        "# 章节说明\n"
        "- 解释一\n"
        "```python\n"
        "print('正文')\n"
        "```\n"
    )
    findings = sepia.detect_surface_violations(text)
    kinds = [kind for kind, _ in findings]
    assert "章节尾标记" in kinds
    assert "AI元话语" in kinds
    assert "标题式解释" in kinds
    assert "Markdown格式" in kinds
    assert "代码块" in kinds
    assert sepia.detect_surface_violations("李羽推门进屋，屋里没人。") == []


def test_surface_findings_are_bounded_and_formatted():
    text = "\n".join(f"- 条目 {i}" for i in range(30))
    findings = sepia.detect_surface_violations(text)
    assert len(findings) == 12
    assert all(len(excerpt) <= 80 for _, excerpt in findings)
    formatted = sepia.format_surface_findings(findings)
    assert formatted.startswith("表层预检：")
    assert formatted.count("[Markdown格式]") == 12
    assert sepia.format_surface_findings([]) == ""


def test_parse_sepia_problems_json_tolerant_whitelist_truncate_and_sort():
    long_quote = "引句" * 100
    long_problem = "问题" * 150
    payload = {
        "sepia_problems": [
            {"type": "表层", "quote": "表层引句", "problem": "混入标题", "suggestion": "删掉"},
            {"type": "未知", "problem": "过滤"},
            {"type": "话语", "quote": long_quote, "problem": long_problem},
            {"type": "叙事", "problem": "转折过于顺滑", "suggestion": "保留一个未回收细节"},
            {"type": "叙事"},
            "not a dict",
        ]
    }
    out = ca.parse_sepia_problems_json("前缀\n" + json.dumps(payload, ensure_ascii=False) + "\n后缀")
    assert [item["type"] for item in out] == ["叙事", "话语", "表层"]
    assert len(out[1]["quote"]) == 120
    assert len(out[1]["problem"]) == 200
    assert out[0]["suggestion"] == "保留一个未回收细节"
    assert ca.parse_sepia_problems_json('{"sepia_problems":"bad"}') == []


def test_format_analysis_reply_keeps_sepia_separate_from_legacy_problems():
    reply = ca._format_analysis_reply(
        [("AI元话语", "写完了")],
        ["字数：约 10 字"],
        [{"type": "逻辑", "quote": "甲死了", "problem": "后文又出现", "suggestion": "核对时间线"}],
        "1个事件，节奏正常",
        "甲发现门后有人",
        [],
        "1",
        [{"type": "话语", "quote": "空气凝固", "problem": "感官模板化", "suggestion": "改用动作表现"}],
    )
    assert "表层预检" in reply
    assert "【逻辑】" in reply
    assert "Sepia 发现 1 处" in reply
    assert "【话语】" in reply
    assert reply.index("【逻辑】") < reply.index("【话语】")
