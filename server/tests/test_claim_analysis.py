"""声明级比对测试：把多篇报道拆成声明、找冲突、区分事实/主张/定性。

关键要防的错：把某方主张或自媒体定性当事实、把两个人的属性对齐比对、
把进展性时间差当冲突、把事发时间和报道时间混淆。
"""
import asyncio
from types import SimpleNamespace

from app.chat import claim_analysis as ca


def _item(source, title, summary=""):
    return {"source": source, "title": title, "summary": summary}


# ── 规则抽取 ────────────────────────────────────────────────

def test_rule_extracts_hard_facts():
    cs = ca.extract_claims_rule([
        _item("凤凰网", "福建女子称被4岁男童摸臀", "8月26日事发，已三次调解"),
    ])
    keys = {c.key: c.value for c in cs.claims}
    assert keys.get("硬事实/时间/事发") == "8月26日"
    assert keys.get("硬事实/数字/调解") == "3次"
    assert keys.get("硬事实/身份/籍贯") == "福建"


def test_event_time_and_report_time_not_confused():
    """事发时间 vs 报道时间必须分开，否则会被误判为时间冲突。"""
    cs = ca.extract_claims_rule([
        _item("新京报", "男童父母准备应诉", "9月8日报道，事件8月26日发生"),
    ])
    subs = {c.sub: c.value for c in cs.claims if c.dimension == "时间"}
    assert subs.get("事发") == "8月26日"
    assert subs.get("报道") == "9月8日"
    rep = ca.analyze_claims(cs)
    # 同一篇内的事发/报道不同 sub，不算冲突
    assert rep.conflict_count == 0


def test_age_not_extracted_by_rule_to_avoid_multi_subject_conflict():
    """一篇里有 4岁男童 + 19岁女子，规则分不清主体，不应抽年龄。"""
    cs = ca.extract_claims_rule([
        _item("A", "19岁女子与4岁男童纠纷", "两人在餐厅发生冲突"),
    ])
    assert not [c for c in cs.claims if c.sub == "年龄"]


def test_lone_source_marked_single():
    cs = ca.extract_claims_rule([
        _item("凤凰网", "福建女子小熊", "8月26日事发"),
        _item("潇湘晨报", "长沙女子与男童纠纷", "8月26日中午发生"),
    ])
    origin = [c for c in cs.claims if c.sub == "籍贯"]
    assert len(origin) == 1
    assert origin[0].single is True          # 只有凤凰网提"福建"


def test_shared_fact_merges_sources_not_conflict():
    """两家都说 8月26日事发 → 合并信源、不是冲突。"""
    cs = ca.extract_claims_rule([
        _item("凤凰网", "事件回顾", "8月26日事发"),
        _item("潇湘晨报", "事件回顾", "8月26日事发"),
    ])
    fa = [c for c in cs.claims if c.sub == "事发"]
    assert len(fa) == 1
    assert set(fa[0].sources) == {"凤凰网", "潇湘晨报"}
    assert fa[0].single is False


# ── 冲突检测 ────────────────────────────────────────────────

def test_hard_fact_conflict_detected():
    cs = ca.extract_claims_rule([
        _item("凤凰网", "三次调解", "已三次调解未果"),
        _item("潇湘晨报", "两次调解", "警方两次调解"),
    ])
    conflicts = ca.detect_conflicts(cs)
    tuiao = [c for c in conflicts if c.sub == "调解"]
    assert tuiao and tuiao[0].kind == "冲突"


def test_judgment_conflict_is_each_their_own():
    """定性分歧属"各执一词"，不是硬冲突。"""
    cs = ca.ClaimSet(claims=[
        ca.Claim(ca.LAYER_JUDGMENT, "性质", "认定", "摸臀",
                 attribution=ca.ATTR_PARTY, sources=["凤凰网"]),
        ca.Claim(ca.LAYER_JUDGMENT, "性质", "认定", "奔跑中不慎触碰",
                 attribution=ca.ATTR_PARTY, sources=["新京报"]),
    ])
    conflicts = ca.detect_conflicts(cs)
    assert conflicts and conflicts[0].kind == "各执一词"


def test_disposal_time_diff_is_progress_not_conflict():
    """准备诉讼(9/8) vs 已起诉(9/9) → 进展，不是冲突。"""
    cs = ca.ClaimSet(claims=[
        ca.Claim(ca.LAYER_DISPOSAL, "法律", "进展", "准备诉讼",
                 sources=["A"], reported_at="9月8日"),
        ca.Claim(ca.LAYER_DISPOSAL, "法律", "进展", "已起诉",
                 sources=["B"], reported_at="9月9日"),
    ])
    conflicts = ca.detect_conflicts(cs)
    assert conflicts and conflicts[0].is_progress


# ── 渲染 ────────────────────────────────────────────────────

def test_render_flags_third_party_definition():
    """自媒体定性不能当事实，渲染要明说。"""
    cs = ca.ClaimSet(claims=[
        ca.Claim(ca.LAYER_JUDGMENT, "性质", "认定", "猥亵",
                 attribution=ca.ATTR_THIRD, sources=["公众号"]),
    ])
    text = ca.render(cs, ca.detect_conflicts(cs))
    assert "猥亵" in text and "不能采信" in text


def test_render_empty_when_no_conflict_no_flag():
    cs = ca.ClaimSet(claims=[
        ca.Claim(ca.LAYER_HARD, "时间", "事发", "8月26日",
                 sources=["A", "B"]),
    ])
    assert ca.render(cs, ca.detect_conflicts(cs)) == ""


# ── LLM 抽取解析与降级 ──────────────────────────────────────

def test_parse_claims_tolerates_fenced_json():
    text = '```json\n{"claims":[{"layer":"定性","dimension":"性质","sub":"认定","value":"摸臀","attribution":"某方主张","source":"凤凰网"}]}\n```'
    cs = ca.parse_claims(text)
    assert len(cs.claims) == 1
    assert cs.claims[0].attribution == ca.ATTR_PARTY
    assert cs.extracted_by_llm is True


def test_parse_claims_rejects_bad_json():
    assert ca.parse_claims("这不是JSON").claims == []
    assert ca.parse_claims("").claims == []


def test_parse_claims_drops_invalid_layer():
    text = '{"claims":[{"layer":"乱写","value":"x"},{"layer":"硬事实","dimension":"时间","sub":"事发","value":"8月26日"}]}'
    cs = ca.parse_claims(text)
    assert len(cs.claims) == 1
    assert cs.claims[0].value == "8月26日"


def test_llm_extraction_failure_degrades_to_empty():
    """LLM 调用抛错 → 返回空集，调用方降级到规则结果。"""
    class _LLM:
        async def chat(self, *a, **k):
            raise RuntimeError("boom")

    runtime = SimpleNamespace(
        settings=SimpleNamespace(claim_analysis_budget=5.0, claim_analysis_max_tokens=800,
                                 reflection_review_model="", llm_model="m"),
        llm=_LLM(),
        logger=SimpleNamespace(warning=lambda *a, **k: None),
    )
    cs = asyncio.run(ca.extract_claims_llm([_item("A", "标题")], runtime))
    assert cs.claims == []


def test_merge_combines_rule_and_llm():
    rule = ca.extract_claims_rule([_item("凤凰网", "x", "8月26日事发")])
    llm = ca.parse_claims(
        '{"claims":[{"layer":"定性","dimension":"性质","sub":"认定","value":"摸臀","attribution":"某方主张","source":"凤凰网"}]}'
    )
    merged = ca.merge(rule, llm)
    keys = {c.key for c in merged.claims}
    assert "硬事实/时间/事发" in keys
    assert "定性/性质/认定" in keys
