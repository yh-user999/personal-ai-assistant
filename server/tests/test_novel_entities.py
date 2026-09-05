"""小说实体索引测试：意图识别、命名句定位、块内裁剪、跨类去重、完整度报告。

背景（实测数据，《寂静杀戮》1936 块）：
- 向量搜「命丛有哪些」top3 全是无关 PDF，小说排第四（sim 0.023 vs 0.025）
- FTS5 搜类名「命丛」命中 308 块 = 15.9% 精度，等于没筛
- FTS5 搜专名「银河灵潮」命中 1 块 = ~100%
所以必须建专名索引，把类名匹配转成专名匹配。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

from app.models.database import connect
from app.services import novel_entities as ne

BOOK = "测试小说"


def _seed_chunk(idx: int, content: str, book: str = BOOK) -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO knowledge_chunks (doc_name, chunk_index, content, created_at) "
        "VALUES (?, ?, ?, '2026-09-01T00:00:00+00:00')",
        (book, idx, content),
    )
    conn.commit()


# ── 第 0 层：意图识别 ─────────────────────────────────────

@pytest.mark.parametrize("query", [
    "小说里出现过哪些命丛和命图",   # 真实问法："过哪些"而非"有哪些"
    "命丛有哪些",
    "列举一下命图",
    "命丛一共多少种",
    "把命图整理一下",
    "命丛清单",
    "所有命图",
    "命丛都有什么",
])
def test_enum_intent_detected(query):
    assert ne.detect_enum_intent(query), f"枚举意图漏判: {query}"


@pytest.mark.parametrize("query", [
    "命丛是什么意思",     # 定义式：要解释不要清单
    "什么是命图",
    "命丛是啥",
    "跟我说说命丛",       # 保守判定：不明确的枚举词不触发
    "夜海怎么修炼",
    "今天天气不错",
])
def test_enum_intent_not_triggered(query):
    assert not ne.detect_enum_intent(query), f"误判为枚举: {query}"


def test_detect_kinds():
    assert ne.detect_kinds("哪些命丛和命图") == ["命丛", "命图"]
    assert ne.detect_kinds("哪些命丛") == ["命丛"]
    assert ne.detect_kinds("今天天气") == []


# ── 命名句定位：把 308 块缩到几十块 ────────────────────────

def test_find_naming_blocks(db):
    _seed_chunk(1, "这个命丛，被称之为‘夜海’。夜海是失传的命丛之一。")
    _seed_chunk(2, "他看着命丛发呆，什么也没想。")  # 重复提及，无命名句
    blocks = ne.find_naming_blocks(BOOK, "命丛")
    assert len(blocks) == 1, "无命名句的块不该进候选"
    assert blocks[0]["chunk_index"] == 1
    assert "夜海" in blocks[0]["hints"]


def test_naming_hints_respect_distance(db):
    """类名与专名距离过远时不算——书里到处有与命丛无关的命名句。"""
    far = "这里有个命丛。" + "无关内容" * 20 + "那座山被称之为南圣门。"
    _seed_chunk(1, far)
    blocks = ne.find_naming_blocks(BOOK, "命丛")
    hints = blocks[0]["hints"] if blocks else []
    assert "南圣门" not in hints, "超出窗口的命名句不该被采纳"


@pytest.mark.parametrize("name,ok", [
    ("夜海", True), ("银河灵潮", True), ("鬼眼黄泉天", True),
    ("什么", False), ("而已", False), ("之一", False),   # 停用词
    ("的命丛", False),                                  # 助词开头
    ("夜", False),                                      # 太短
    ("命丛ABC", False),                                 # 含非中文
])
def test_plausible_name(name, ok):
    assert ne._plausible_name(name) is ok


# ── 集合性表述：算缺口的依据 ───────────────────────────────

def test_group_mentions_anchored_to_class(db):
    """必须锚定类名——否则「一大口鲜血」「四大天王」会被当成集合
    （实测「一大口鲜血」出现 9 次，比真正的「七大神命丛」还多）。"""
    _seed_chunk(1, "七大神命丛之一，留在你身上浪费了。他吐了一大口鲜血。四大天王到了。")
    groups = ne.find_group_mentions(BOOK)
    assert "七大神命丛" in groups
    assert not any("鲜血" in g or "天王" in g for g in groups)


@pytest.mark.parametrize("text,size", [
    ("七大神命丛", 7), ("四种命图", 4), ("十大无上命图", 10), ("三种命丛", 3),
])
def test_parse_group_size(text, size):
    assert ne.parse_group_size(text) == size


def test_parse_group_size_unknown():
    assert ne.parse_group_size("命丛") is None


# ── 枚举句直抽：补命名句的漏 ───────────────────────────────

def test_enumerated_names_found(db):
    """「这四种命图分别是A，B，C以及D」——命名句模式抓不到这种一次列举。"""
    _seed_chunk(1, "这四种命图分别是鬼眼黄泉天，清净焰光城，夜亡君主以及白帝极光剑。")
    names = ne.find_enumerated_names(BOOK, "命图")
    for expect in ("鬼眼黄泉天", "清净焰光城", "夜亡君主", "白帝极光剑"):
        assert expect in names, f"枚举句漏抽: {expect}"


def test_enumerated_needs_two_items(db):
    """单项不算列举——叙事句容易误命中。"""
    _seed_chunk(1, "他的命图是鬼眼黄泉天。")
    assert len(ne.find_enumerated_names(BOOK, "命图")) < 2


# ── 第 2 层：块内裁剪 ─────────────────────────────────────

def test_extract_snippet_targets_sentence(db):
    """治的是"注入 5638 字符却全是马匹嘶鸣的描写"——只取含专名的句子。"""
    content = ("众人发出了惊叹。马儿嘶鸣着朝后退去。" * 10
               + "这个命丛，被称之为夜海，是失传命丛之一。"
               + "他转身离开了。" * 10)
    snippet, _score = ne.extract_snippet(content, "夜海")
    assert "夜海" in snippet
    assert len(snippet) <= ne.MAX_SNIPPET_CHARS
    # 关键是密度：原块 400+ 字里只有一句含专名，裁剪后应大幅缩短。
    # 不断言"完全不含叙事"——CONTEXT_SENTENCES=1 会带前后各一句，这是有意的
    # （只留命中句会丢失上下文，"被称之为夜海"前一句往往是"你的命丛在左眼里"）。
    assert len(snippet) < len(content) / 3


def test_snippet_scores_definitions_higher(db):
    """「一共分为…」这类定义句权重应高于叙事句。"""
    _, narrative = ne.extract_snippet("他看着夜海发呆。天色渐晚。", "夜海")
    _, definition = ne.extract_snippet("道术一共分为炼夜海，修天宫，共有四步。", "夜海")
    assert definition > narrative


def test_extract_snippet_no_match(db):
    assert ne.extract_snippet("完全无关的内容", "夜海") == ("", 0.0)


# ── 实体表 CRUD 与优先级 ──────────────────────────────────

def test_upsert_and_list(db):
    ne.upsert_entity(BOOK, "夜海", "命丛", group_name="七大神命丛", first_chunk=40)
    ne.upsert_entity(BOOK, "银河灵潮", "命图", first_chunk=1708)
    assert len(ne.list_entities(BOOK)) == 2
    assert len(ne.list_entities(BOOK, kind="命丛")) == 1


def test_upsert_idempotent(db):
    a = ne.upsert_entity(BOOK, "夜海", "命丛")
    b = ne.upsert_entity(BOOK, "夜海", "命丛")
    assert a == b
    assert len(ne.list_entities(BOOK)) == 1


def test_verified_not_downgraded_by_reextraction(db):
    """用户确认过的不该被后续自动抽取覆盖。"""
    ne.upsert_entity(BOOK, "夜海", "命丛")
    ne.verify_entity(BOOK, "夜海", "命丛", note="我改成了双眼")
    ne.upsert_entity(BOOK, "夜海", "命丛", verified=0)  # 重跑抽取
    ent = ne.list_entities(BOOK)[0]
    assert ent["verified"] == 1
    assert ent["note"] == "我改成了双眼"


def test_user_note_overrides_source_text(db):
    """修订优先于原文——检索注入里要标明"你修订过"。"""
    _seed_chunk(1, "这个命丛被称之为夜海，位于左眼。")
    ne.upsert_entity(BOOK, "夜海", "命丛")
    ne.verify_entity(BOOK, "夜海", "命丛", note="改设定：位于双眼")
    ctx = ne.build_entity_context("哪些命丛", book=BOOK)
    assert "你修订过" in ctx and "位于双眼" in ctx


# ── 跨类去重 ──────────────────────────────────────────────

def test_enumeration_beats_cooccurrence(db):
    """「鬼眼黄泉天」是命图，但它需要 81 个命丛，与"命丛"共现远多于"命图"
    （实测 85 vs 31）。枚举句是直接证据，必须优先于共现频次。"""
    _seed_chunk(1, "这四种命图分别是鬼眼黄泉天，清净焰光城，夜亡君主以及白帝极光剑。")
    for i in range(2, 8):  # 制造大量"鬼眼黄泉天 × 命丛"共现
        _seed_chunk(i, "鬼眼黄泉天需要八十一个命丛，命丛越多越强，命丛难寻。")
    ne.upsert_entity(BOOK, "鬼眼黄泉天", "命丛")
    ne.upsert_entity(BOOK, "鬼眼黄泉天", "命图")
    removed = ne.resolve_cross_kind_duplicates(BOOK)
    kinds = [e["kind"] for e in ne.list_entities(BOOK)]
    assert kinds == ["命图"], f"应保留命图，实际 {kinds}；removed={removed}"


def test_cross_kind_skips_verified(db):
    """用户确认过的记录不自动删。"""
    ne.upsert_entity(BOOK, "夜海", "命丛")
    ne.upsert_entity(BOOK, "夜海", "命图")
    ne.verify_entity(BOOK, "夜海", "命图")
    ne.resolve_cross_kind_duplicates(BOOK)
    assert len(ne.list_entities(BOOK)) == 2


# ── 第 4 层：完整度报告 ───────────────────────────────────

def test_context_reports_gap(db):
    """缺口可见才谈得上诚实——原文说七个而只收录一个时必须说明。"""
    _seed_chunk(1, "这个命丛被称之为夜海。七大神命丛之一。")
    ne.upsert_entity(BOOK, "夜海", "命丛", group_name="七大神命丛", first_chunk=1)
    ctx = ne.build_entity_context("哪些命丛", book=BOOK)
    assert "七大神命丛" in ctx and "应有 7 个" in ctx
    assert "仍缺 6 个" in ctx
    assert "不要凑数" in ctx


def test_context_marks_entity_without_description(db):
    """实体表有名字但正文找不到描述时要标注，不能装作有内容。"""
    ne.upsert_entity(BOOK, "不存在的命丛", "命丛")
    ctx = ne.build_entity_context("哪些命丛", book=BOOK)
    assert "正文未找到描述" in ctx


def test_context_empty_on_non_enum_intent(db):
    ne.upsert_entity(BOOK, "夜海", "命丛")
    assert ne.build_entity_context("夜海怎么修炼", book=BOOK) == ""


def test_context_empty_without_entities(db):
    assert ne.build_entity_context("哪些命丛", book=BOOK) == ""


def test_context_respects_budget(db):
    """预算裁剪：不能因为实体多就把 prompt 撑爆。"""
    long_text = "这个命丛被称之为夜海。" + "详细描述内容" * 200
    for i in range(1, 15):
        _seed_chunk(i, long_text.replace("夜海", f"命丛{i:02d}"))
        ne.upsert_entity(BOOK, f"命丛{i:02d}", "命丛", first_chunk=i)
    ctx = ne.build_entity_context("哪些命丛", book=BOOK)
    assert len(ctx) < ne.TOTAL_BUDGET_CHARS * 2, f"注入超预算: {len(ctx)}"


def test_faction_trigger_words_avoid_single_char(db):
    """势力触发词不能用单字「宗」「教」——会命中"宗旨""教训"这类无关词。

    实测用单字时候选里混进了"孙悟空""恶意""封印"。
    """
    for w in ENTITY_KINDS_FACTION:
        assert len(w) >= 2, f"势力触发词过短会误命中: {w}"


ENTITY_KINDS_FACTION = ne.ENTITY_KINDS["势力"]


def test_extract_concurrency_bounded():
    """并发度必须有上限：一次性 gather 60 个请求会被 API 限速或拒绝，
    而串行 58 块会跑十几分钟（实测超 15 分钟未完）。"""
    assert 1 < ne.EXTRACT_CONCURRENCY <= 12


def test_dedupe_removes_repeated_facts(db):
    """同一段设定被逐字重复引用时只留一份。"""
    same = "这个命丛被称之为夜海，是失传命丛之一，位于左眼中。"
    for i in range(1, 6):
        _seed_chunk(i, same)
    ne.upsert_entity(BOOK, "夜海", "命丛", first_chunk=1)
    ctx = ne.build_entity_context("哪些命丛", book=BOOK)
    assert ctx.count("失传命丛之一") <= ne.MAX_SNIPPETS_PER_ENTITY
