"""RAG 知识库测试：切块器 + 检索（向量环境可用时）。"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_DIMENSION", "1024")

from app.config import settings  # noqa: E402
from app.core.chunker import chunk_text  # noqa: E402
from app.models.database import init_db, reset_connections  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """独立临时库。原实现靠 DB_PATH 环境变量隔离（无效——settings 是 lru_cache
    单例），三个用例里的 DELETE FROM knowledge_chunks / novel_facts 一直跑在
    生产库上，曾清掉 3600 个真实知识库块。"""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    reset_connections()
    init_db()
    yield
    reset_connections()


def test_chunk_by_paragraphs():
    text = "\n\n".join(f"第{i}段内容" * 10 for i in range(5))
    chunks = chunk_text(text, chunk_size=100, overlap=10)
    assert len(chunks) >= 2
    assert all(len(c) <= 130 for c in chunks)  # 含重叠余量


def test_chunk_long_paragraph_split():
    text = "长段落测试内容" * 200  # 单个超长段落
    chunks = chunk_text(text, chunk_size=100, overlap=20)
    assert len(chunks) >= 10
    # 重叠验证：相邻块有交叠内容
    assert chunks[0][-20:] == chunks[1][:20]


def test_chunk_empty_text():
    assert chunk_text("") == []
    assert chunk_text("\n\n\n") == []


def test_chunk_preserves_order():
    text = "\n\n".join(f"段落{i} " * 30 for i in range(3))
    chunks = chunk_text(text, chunk_size=200, overlap=0)
    joined = "".join(chunks)
    assert "段落0" in joined and "段落2" in joined


# ── 邻域扩展（6.18 后：小说问答情节完整性）────────────────

def test_expand_chunks_merges_neighborhood():
    from app.core import knowledge
    from app.models.database import connect

    # 库隔离与建表由 autouse 的 fresh_db 负责（临时库本就是空的）
    conn = connect()
    for i in range(6):
        conn.execute(
            "INSERT INTO knowledge_chunks (doc_name, chunk_index, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("小说-测试", i, f"第{i}段内容", "2026-01-01T00:00:00+00:00"),
        )
    conn.commit()
    conn.close()

    hits = [
        {"doc_name": "小说-测试", "chunk_index": 3, "content": "第3段内容", "similarity": 0.8},
        # 落在扩展区间内的命中应被去重
        {"doc_name": "小说-测试", "chunk_index": 4, "content": "第4段内容", "similarity": 0.7},
        # 区间外的保持原样
        {"doc_name": "小说-测试", "chunk_index": 9, "content": "第9段内容", "similarity": 0.6},
    ]
    out = knowledge.expand_chunks(hits, radius=2)
    assert len(out) == 2  # 首条扩展 + 区间外的一条
    assert out[0]["expanded"] is True
    assert out[0]["chunk_index"] == "1-5"
    assert "第1段内容" in out[0]["content"] and "第5段内容" in out[0]["content"]
    assert out[1]["chunk_index"] == 9
    # 注入格式带"剧情片段"标注
    text = knowledge.format_knowledge_injection(out)
    assert "剧情片段" in text and "片段" in text


def test_expand_chunks_empty():
    from app.core import knowledge

    assert knowledge.expand_chunks([]) == []



def test_bm25_idf_saturation():
    """词频饱和：稀有词证据块应压过高频词霸榜块。"""
    from app.core import knowledge
    from app.models.database import connect

    # 库隔离由 autouse 的 fresh_db 负责
    conn = connect()
    cur0 = conn.execute(
        "INSERT INTO knowledge_chunks (doc_name, chunk_index, content, created_at) "
        "VALUES ('测试', 0, ?, '2026-01-01T00:00:00+00:00')",
        ("命丛 " * 50,),  # 高频霸榜块
    )
    cur1 = conn.execute(
        "INSERT INTO knowledge_chunks (doc_name, chunk_index, content, created_at) "
        "VALUES ('测试', 1, ?, '2026-01-01T00:00:00+00:00')",
        ("蜃宗挖走了左志诚的命丛",),  # 稀有词证据块
    )
    # FTS 同步（v0.3.2 起检索走 FTS5 倒排，直插 SQL 需手动补索引行）
    knowledge._fts_sync_doc(conn, "测试", ["命丛 " * 50], [cur0.lastrowid])
    knowledge._fts_sync_doc(conn, "测试", ["蜃宗挖走了左志诚的命丛"], [cur1.lastrowid])
    conn.commit()
    conn.close()
    hits = knowledge._bm25_rank("左志诚被谁挖走了命丛", top_k=10)
    assert hits[0]["chunk_index"] == 1  # 证据块必须排第一


def test_novel_facts_match():
    from app.core import knowledge
    from app.models.database import connect

    # 库隔离由 autouse 的 fresh_db 负责
    conn = connect()
    conn.execute(
        "INSERT INTO novel_facts (book, keywords, content, created_at) "
        "VALUES ('小说-x', '左志诚,左擎苍', '设定A', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    assert knowledge.get_novel_facts("左志诚被谁挖走了命丛") == ["设定A"]
    assert knowledge.get_novel_facts("今天天气") == []
