"""知识库主动提示测试：触发条件与防打扰约束。

这个功能最大的风险是变成打扰（"你还没问我这个呢"），所以测试重点在
**不该触发的情况**：已经在直接提问时、闲聊时、冷却期内。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

from app.models.database import connect
from app.services import knowledge_hint as kh


@pytest.fixture(autouse=True)
def clean_cooldown():
    kh.reset()
    yield
    kh.reset()


def _seed_novel(term: str, n: int, doc: str = "小说-测试") -> None:
    """灌入含该词的块，并把该词登记为实体。

    登记实体是必须的——_terms 只认实体表里的专名（中文没有分词器，
    按字数硬切会产出「左志诚这段不」这类搜不到东西的片段）。
    """
    from app.services.novel_entities import upsert_entity

    conn = connect()
    for i in range(n):
        conn.execute(
            "INSERT INTO knowledge_chunks (doc_name, chunk_index, content, created_at, domain) "
            "VALUES (?, ?, ?, '2026-09-01T00:00:00+00:00', 'novel')",
            (doc, i, f"包含{term}的剧情内容第{i}段"),
        )
    conn.commit()
    upsert_entity(doc, term, "人物")


# ── 应触发 ────────────────────────────────────────────────

def test_hints_when_topic_matches_library(db):
    _seed_novel("左志诚", 50)
    out = kh.build_hint("今天写小说卡住了，左志诚这段不好写")
    assert out and "小说-测试" in out


def test_hint_includes_dont_force_instruction(db):
    """提示必须带"接不上就别提"——否则会变成硬推销。"""
    _seed_novel("左志诚", 50)
    out = kh.build_hint("左志诚这段不好写")
    assert "别提" in out
    assert "不要罗列" in out, "不该让她直接倒资料"


# ── 不该触发（防打扰是重点）────────────────────────────────

@pytest.mark.parametrize("query", [
    "有哪些左志诚的资料",   # 已经在问了，检索会处理
    "左志诚是什么人",
    "说说左志诚",
    "查一下左志诚",
])
def test_no_hint_when_already_asking(db, query):
    """已经在直接提问时提示是废话——检索本来就会用库。"""
    _seed_novel("左志诚", 50)
    assert kh.build_hint(query) == "", f"重复提示: {query}"


@pytest.mark.parametrize("query", [
    "今天天气不错",
    "我今天有点累",
    "午饭吃了碗面",
    "帮我打开F盘",
])
def test_no_hint_for_chitchat(db, query):
    """闲聊不该触发——命中门槛就是为了挡这类（3 块太低时误触发过）。"""
    _seed_novel("左志诚", 50)
    assert kh.build_hint(query) == "", f"闲聊误触发: {query}"


def test_no_hint_below_threshold(db):
    """库里内容太少不值得提。"""
    _seed_novel("左志诚", 5)   # 低于 MIN_RELATED_CHUNKS
    assert kh.build_hint("左志诚这段不好写") == ""


def test_per_doc_cooldown(db):
    """同一文档短期内不重复提示。"""
    _seed_novel("左志诚", 50)
    assert kh.build_hint("左志诚这段不好写")
    assert kh.build_hint("左志诚那段也要改") == "", "冷却期内不该再提"


def test_global_cooldown(db):
    """任何提示之间要有间隔，一天最多几次，别话痨。"""
    _seed_novel("左志诚", 50, doc="小说-甲")
    _seed_novel("蜃宗", 50, doc="小说-乙")
    assert kh.build_hint("左志诚这段")
    assert kh.build_hint("蜃宗那段") == "", "全局冷却应挡住不同文档的连续提示"


def test_cooldown_expires(db):
    """冷却过期后可以再提。"""
    import time

    _seed_novel("左志诚", 50)
    assert kh.build_hint("左志诚这段")
    later = time.time() + kh.HINT_COOLDOWN_SECONDS + kh.GLOBAL_COOLDOWN_SECONDS + 1
    assert kh.build_hint("左志诚这段", now=later)


def test_project_docs_not_hinted(db):
    """项目文档是他自己写的，不需要提示。"""
    conn = connect()
    for i in range(50):
        conn.execute(
            "INSERT INTO knowledge_chunks (doc_name, chunk_index, content, created_at, domain) "
            "VALUES ('OPS', ?, '包含部署流程的内容', '2026-09-01T00:00:00+00:00', 'project_doc')",
            (i,),
        )
    conn.commit()
    assert kh.build_hint("部署流程有点忘了") == ""


def test_empty_message(db):
    assert kh.build_hint("") == ""
    assert kh.build_hint("   ") == ""


def test_terms_only_uses_known_entities(db):
    """抽词只认实体表里的专名，不做任意切分。

    中文没有分词器，按固定字数切会产出「左志诚这段不」这类搜不到东西的片段
    （实测切出的 4 个词全部 0 命中），或反过来乱撞命中（「反代」的碎片匹配
    到了小说）。实体表的 160 个专名天然是"库里成体系的话题词"。
    """
    _seed_novel("左志诚", 40)
    terms = kh._terms("今天写小说卡住了，左志诚这段不好写")
    assert terms == ["左志诚"], f"不该产出切碎片段: {terms}"


def test_terms_empty_without_entities(db):
    """实体表为空时抽不出中文词——保守而非乱猜。"""
    assert kh._terms("今天写小说卡住了") == []
