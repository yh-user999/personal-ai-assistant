"""记忆核心单元测试：精确去重 + 关键词检索兜底 + 评分。
不依赖真实 Embedding API（无 key 时 embed 快速失败，自动走兜底路径）。
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")
os.environ.setdefault("DB_PATH", "/tmp/test_assistant_memory.db")

from app.core import memory  # noqa: E402
from app.models.database import init_db  # noqa: E402


@pytest.fixture(autouse=True)
def no_network_embedding(monkeypatch):
    """测试环境禁用真实 Embedding 网络调用：embed 快速失败，走兜底路径。"""

    async def fake_embed(texts):
        raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.core.embedding.embed", fake_embed)


def setup_function():
    db_file = Path("/tmp/test_assistant_memory.db")
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_file) + suffix).unlink(missing_ok=True)
    init_db()


def test_duplicate_message_not_reinserted():
    first = asyncio.run(memory.write_message("user", "测试去重消息"))
    second = asyncio.run(memory.write_message("user", "测试去重消息"))
    assert isinstance(first, int) and first > 0
    assert second is None  # 24h 内重复消息被跳过


def test_different_message_reinserted():
    asyncio.run(memory.write_message("user", "另一条消息A"))
    second = asyncio.run(memory.write_message("user", "另一条消息B"))
    assert isinstance(second, int) and second > 0


def test_keyword_search_fallback():
    """无向量时检索退化为关键词兜底，仍能命中。"""
    asyncio.run(memory.write_message("user", "RAG向量化性能调优记录"))
    results = asyncio.run(memory.search("RAG向量化"))
    assert len(results) >= 1
    assert any("RAG" in r["content"] for r in results)


def test_injection_format():
    mems = [{"ts": "2026-08-20T00:00:00+00:00", "content": "内容A", "summary": ""}]
    text = memory.format_injection(mems)
    assert "[记忆] 2026-08-20: 内容A" == text


def test_bm25_memories_rare_term_wins():
    """6.22：记忆 BM25 稀有词加权——'杀人变强'应压过高频闲聊。"""
    import os
    os.environ.setdefault("DB_PATH", "/tmp/test_memory_hybrid.db")
    from app.core import memory
    from app.models.database import connect, init_db

    init_db()
    conn = connect()
    conn.execute("DELETE FROM memories")
    for i in range(6):
        conn.execute(
            "INSERT INTO memories (sender, content, summary, topics, ts, importance) "
            "VALUES ('user', ?, '', '[]', '2026-08-28T00:00:00+00:00', 1.0)",
            (f"今天天气不错，继续写代码 {i}",),
        )
    conn.execute(
        "INSERT INTO memories (sender, content, summary, topics, ts, importance) "
        "VALUES ('user', '李羽的能力设定是杀人变强', '', '[]', '2026-08-28T00:00:00+00:00', 1.0)"
    )
    conn.commit()
    conn.close()
    hits = memory._bm25_memories("李羽的能力是杀人变强吗", top_k=3)
    assert hits, "应有命中"
    assert "杀人变强" in hits[0]["content"]


def test_deep_keyword_search_matches_grams():
    import os
    os.environ.setdefault("DB_PATH", "/tmp/test_memory_hybrid.db")
    from app.core import memory
    from app.models.database import connect, init_db

    init_db()
    conn = connect()
    conn.execute("DELETE FROM memories")
    conn.execute(
        "INSERT INTO memories (sender, content, summary, topics, ts, importance) "
        "VALUES ('user', '少爷的背景势力是地方豪强', '', '[]', '2026-08-28T00:00:00+00:00', 1.0)"
    )
    conn.execute(
        "INSERT INTO memories (sender, content, summary, topics, ts, importance) "
        "VALUES ('assistant', '好的，设定记下了', '', '[]', '2026-08-28T00:00:00+00:00', 1.0)"
    )
    conn.commit()
    conn.close()
    hits = memory.deep_keyword_search("少爷的背景势力", top_k=3)
    assert hits and "地方豪强" in hits[0]["content"]
