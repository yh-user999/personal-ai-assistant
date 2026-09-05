"""知识库分域测试：文档分类、意图路由、严格分域 + 全域兜底。

背景（实测 3602 块、19 个文档混检）：
- 「李羽的能力是什么」命中 6/6 全错（4 块另一本小说 + 2 块 LESSONS.md）
- 「命丛有哪些」命中 3/6 无关（反代教程 PDF / AI 模板 / 名词焦虑 PDF）
根因是 embedding 各向异性：所有块相似度塌在 0.023~0.025（0.002 宽），
向量对"相关/无关"没有区分力，只能靠元数据过滤兜住。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

from app.models.database import connect
from app.services import knowledge_domain as kd


def _seed(doc: str, idx: int, content: str, domain: str = "") -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO knowledge_chunks (doc_name, chunk_index, content, created_at, domain) "
        "VALUES (?, ?, ?, '2026-09-01T00:00:00+00:00', ?)",
        (doc, idx, content, domain),
    )
    conn.commit()


def _seed_fact(subject: str, predicate: str, obj: str) -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO facts (user_id, subject, predicate, object, updated_at) "
        "VALUES ('owner', ?, ?, ?, '2026-09-01T00:00:00+00:00')",
        (subject, predicate, obj),
    )
    conn.commit()


def _seed_entity(book: str, name: str, kind: str) -> None:
    from app.services.novel_entities import upsert_entity

    upsert_entity(book, name, kind)


# ── 文档 → 域 ─────────────────────────────────────────────

@pytest.mark.parametrize("doc,domain", [
    ("小说-寂静杀戮", kd.DOMAIN_NOVEL),
    ("小说-食物链顶端的男人", kd.DOMAIN_NOVEL),
    ("简历（脱敏版）", kd.DOMAIN_RESUME),
    ("文档-简历优化版-阿里云运维工程师", kd.DOMAIN_RESUME),
    ("小白零基础反代教程.pdf", kd.DOMAIN_MANUAL),
    ("小白新手必看【减少名词焦虑】.pdf", kd.DOMAIN_MANUAL),
    ("LESSONS", kd.DOMAIN_PROJECT),
    ("OPS", kd.DOMAIN_PROJECT),
    ("实施方案细则", kd.DOMAIN_PROJECT),
    ("多人支持设计", kd.DOMAIN_PROJECT),
    ("完全无法归类的东西", kd.DOMAIN_OTHER),
])
def test_classify_doc(doc, domain):
    assert kd.classify_doc(doc) == domain


# ── 查询 → 目标域 ─────────────────────────────────────────

def test_book_name_narrows_to_that_book(db):
    _seed("小说-寂静杀戮", 1, "内容", kd.DOMAIN_NOVEL)
    domains, docs = kd.detect_domains("寂静杀戮的主角是谁")
    assert domains == [kd.DOMAIN_NOVEL]
    assert docs == ["小说-寂静杀戮"]


def test_entity_name_narrows_to_that_book(db):
    """专名定位：问《寂静杀戮》的角色不该捞另一本书（实测 4/6 命中来自另一本）。"""
    _seed("小说-寂静杀戮", 1, "夜海是失传命丛", kd.DOMAIN_NOVEL)
    _seed_entity("小说-寂静杀戮", "夜海", "命丛")
    domains, docs = kd.detect_domains("夜海是什么")
    assert domains == [kd.DOMAIN_NOVEL]
    assert docs == ["小说-寂静杀戮"]


def test_class_word_narrows_by_dominance(db):
    """体系词归属：「命丛」在《寂静杀戮》独占 → 只搜那本。

    这是 3/6 PDF 污染的修复点——「命丛有哪些」里没有任何专名。
    """
    for i in range(10):
        _seed("小说-寂静杀戮", i, "命丛的描述", kd.DOMAIN_NOVEL)
    _seed("小说-食物链顶端的男人", 1, "念气与能级", kd.DOMAIN_NOVEL)
    domains, docs = kd.detect_domains("命丛有哪些")
    assert domains == [kd.DOMAIN_NOVEL]
    assert docs == ["小说-寂静杀戮"], "体系词应收窄到独占它的书"


def test_class_word_shared_by_books_not_narrowed(db):
    """两本书都大量出现的体系词不收窄（避免误排除）。"""
    for i in range(10):
        _seed("小说-寂静杀戮", i, "道术修炼", kd.DOMAIN_NOVEL)
        _seed("小说-食物链顶端的男人", i, "道术也有", kd.DOMAIN_NOVEL)
    domains, docs = kd.detect_domains("道术怎么练")
    assert domains == [kd.DOMAIN_NOVEL]
    assert docs == [], "共有体系词不该收窄到单本"


@pytest.mark.parametrize("query,domain", [
    ("RAG 检索怎么优化", kd.DOMAIN_PROJECT),
    ("向量库怎么建索引", kd.DOMAIN_PROJECT),
    ("执行器白名单怎么配", kd.DOMAIN_PROJECT),
    ("我的简历怎么改", kd.DOMAIN_RESUME),
    ("反代教程讲了什么", kd.DOMAIN_MANUAL),
])
def test_term_based_domains(db, query, domain):
    assert kd.detect_domains(query)[0] == [domain]


@pytest.mark.parametrize("query", ["今天天气不错", "帮我打开F盘", "现在几点了"])
def test_undetectable_returns_empty(db, query):
    """判不出域返回空列表——调用方走全域，不是跳过检索。"""
    assert kd.detect_domains(query) == ([], [])


# ── facts 覆盖时跳过检索 ──────────────────────────────────

def test_fact_subject_skips_search(db):
    """李羽是用户自己写的小说角色，设定在 facts（每次必注入），知识库 0 块。

    此时检索只会返回噪声（实测 6/6 全错），应明确跳过而不是退回全域。
    """
    _seed_fact("李羽", "能力", "杀人则变强")
    _seed("小说-食物链顶端的男人", 1, "完全无关的内容", kd.DOMAIN_NOVEL)
    domains, _ = kd.detect_domains("李羽的能力是什么")
    assert kd.SKIP_SEARCH in domains


def test_fact_subject_with_knowledge_still_searches(db):
    """主体在 facts 里但知识库也有大量内容时仍要检索（两边都有的情况）。"""
    _seed_fact("左志诚", "能力", "驯服冥王蛇")
    for i in range(6):
        _seed("小说-寂静杀戮", i, "左志诚驯服了冥王蛇", kd.DOMAIN_NOVEL)
    domains, _ = kd.detect_domains("左志诚驯服了什么")
    assert kd.SKIP_SEARCH not in domains


def test_skip_sentinel_distinct_from_empty():
    """哨兵与空列表必须能区分——少了这个区分，facts 覆盖的主体会退回全域。"""
    assert kd.SKIP_SEARCH not in ("", None)
    assert kd.SKIP_SEARCH not in kd.ALL_DOMAINS


# ── 回填与统计 ────────────────────────────────────────────

def test_backfill_is_idempotent(db):
    _seed("小说-寂静杀戮", 1, "内容")   # domain 留空
    _seed("LESSONS", 1, "内容")
    first = kd.backfill_domains()
    assert first.get(kd.DOMAIN_NOVEL) == 1
    assert first.get(kd.DOMAIN_PROJECT) == 1
    assert kd.backfill_domains() == {}, "已分域的不该重复处理"


def test_backfill_does_not_overwrite(db):
    """已有域标签的行不被覆盖（幂等只填空值）。"""
    _seed("小说-寂静杀戮", 1, "内容", kd.DOMAIN_PROJECT)  # 故意标错
    kd.backfill_domains()
    conn = connect()
    d = conn.execute("SELECT domain FROM knowledge_chunks").fetchone()["domain"]
    conn.close()
    assert d == kd.DOMAIN_PROJECT, "回填不该覆盖已有标签"


def test_class_book_cache_speeds_up(db):
    """书籍归属要缓存：detect_domains 每轮聊天都调，未缓存时对 3520 块做
    全表 LIKE 扫描，实测稳态 48ms/轮（缓存后 9ms）。"""
    import time

    for i in range(20):
        _seed("小说-寂静杀戮", i, "命丛的描述", kd.DOMAIN_NOVEL)
    kd.invalidate_cache()

    t = time.perf_counter()
    first = kd._books_for_class_words(["命丛"])
    cold = time.perf_counter() - t

    t = time.perf_counter()
    second = kd._books_for_class_words(["命丛"])
    warm = time.perf_counter() - t

    assert first == second
    assert warm < cold, f"缓存未生效: cold={cold * 1000:.1f}ms warm={warm * 1000:.1f}ms"


def test_cache_invalidated_by_chunk_count(db):
    """块数变化（灌了新内容）→ 缓存自动失效重算。

    构造：先让《寂静杀戮》独占「命丛」（另一本存在但不含该词，满足
    len(books) > 1 的收窄前提），缓存结果；再往另一本灌入大量同词内容
    打破独占度，验证缓存没有沿用旧答案。
    """
    for i in range(5):
        _seed("小说-寂静杀戮", i, "命丛的设定", kd.DOMAIN_NOVEL)
    _seed("小说-食物链顶端的男人", 0, "念气与能级，不含该体系词", kd.DOMAIN_NOVEL)

    assert kd._books_for_class_words(["命丛"]) == ["小说-寂静杀戮"]

    # 灌入另一本书的大量同词内容 → 归属改变（这里 59/64 = 92% 超过阈值，
    # 独占方反转为另一本）。断言"结果变了"而非"结果为空"——重点是缓存
    # 没有沿用旧答案，具体新答案取决于独占度计算。
    for i in range(1, 60):
        _seed("小说-食物链顶端的男人", i, "命丛也在这本里出现", kd.DOMAIN_NOVEL)
    after = kd._books_for_class_words(["命丛"])
    assert after != ["小说-寂静杀戮"], "块数变化后应重算而非沿用缓存"


def test_invalidate_cache_callable(db):
    kd.invalidate_cache()  # 不该抛异常


def test_ingest_sets_domain(db):
    """入库即定域：新块若留空 domain，分域检索永远找不到它们。"""
    from app.services.knowledge_domain import classify_doc

    assert classify_doc("小说-新书") == kd.DOMAIN_NOVEL
    assert classify_doc("OPS") == kd.DOMAIN_PROJECT


def test_domain_stats(db):
    _seed("小说-寂静杀戮", 1, "a", kd.DOMAIN_NOVEL)
    _seed("小说-寂静杀戮", 2, "b", kd.DOMAIN_NOVEL)
    _seed("OPS", 1, "c", kd.DOMAIN_PROJECT)
    stats = kd.domain_stats()
    assert stats[kd.DOMAIN_NOVEL] == 2
    assert stats[kd.DOMAIN_PROJECT] == 1


# ── 检索层：分域过滤与全域兜底 ─────────────────────────────

def test_bm25_respects_domain_filter(db):
    from app.core import knowledge

    _seed("小说-寂静杀戮", 1, "命丛是吸收灵力的器官", kd.DOMAIN_NOVEL)
    _seed("小白零基础反代教程.pdf", 1, "命丛这个词也出现在教程里", kd.DOMAIN_MANUAL)
    knowledge._fts_backfill(connect())

    all_hits = knowledge._bm25_rank("命丛", 10)
    novel_only = knowledge._bm25_rank("命丛", 10, domains=[kd.DOMAIN_NOVEL])
    assert len(all_hits) >= len(novel_only)
    assert all(h["doc_name"].startswith("小说") for h in novel_only)


def test_bm25_respects_doc_filter(db):
    from app.core import knowledge

    _seed("小说-寂静杀戮", 1, "命丛的设定", kd.DOMAIN_NOVEL)
    _seed("小说-食物链顶端的男人", 1, "命丛也提了一句", kd.DOMAIN_NOVEL)
    knowledge._fts_backfill(connect())

    hits = knowledge._bm25_rank("命丛", 10, docs=["小说-寂静杀戮"])
    assert hits and all(h["doc_name"] == "小说-寂静杀戮" for h in hits)


def test_bm25_weight_raised():
    """BM25 权重从 1.5 提到 3：它只要 8~27ms 且专名精度远高于向量
    （向量 218~1244ms 却区分不出相关与无关）。"""
    from app.core import knowledge

    assert knowledge.BM25_WEIGHT >= 3.0


def test_vector_spread_threshold_configured():
    """向量无区分力检测的阈值必须存在——实测极差只有 0.002。"""
    from app.core import knowledge

    assert 0 < knowledge.VECTOR_SPREAD_MIN <= 0.02
