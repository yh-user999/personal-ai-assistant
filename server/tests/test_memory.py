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
