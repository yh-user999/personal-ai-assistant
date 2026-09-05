"""向量检索端到端测试：验证 sqlite-vec 真实查询通路（MATCH + k + cosine）。

fake embedding 用"正交探针向量"（非零，避免 cosine 零向量 NULL 特例）：
含 RAG 的文本 → [1,0,0,...]；其他 → [0,1,0,...]。
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_DIMENSION", "1024")

from app.config import settings
from app.core import memory
from app.models.database import init_db, reset_connections

DIM = 1024


@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch):
    """确定性 embedding：文本是否含 'RAG' 决定向量方向（正交探针）。"""

    async def fake_embed(texts):
        out = []
        for t in texts:
            v = [0.0] * DIM
            v[0 if "RAG" in t else 1] = 1.0
            out.append(v)
        return out

    monkeypatch.setattr("app.core.embedding.embed", fake_embed)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """独立临时库。原来是 setup_function 删 /tmp 文件 + init_db，但 DB_PATH
    环境变量隔离无效——那三条 "今天调 RAG 向量化性能" 之类的测试记忆一直
    写进生产库（在真实库残骸里就能找到它们）。
    setup_function 拿不到 monkeypatch，故改成 autouse fixture。
    """
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    reset_connections()
    init_db()
    yield
    reset_connections()


def test_vector_knn_ranking():
    """含 RAG 的记忆应与查询同向（distance 0）排第一，无关记忆被相似度过滤。"""
    asyncio.run(memory.write_message("user", "今天调 RAG 向量化性能"))
    asyncio.run(memory.write_message("user", "中午吃了碗面"))
    asyncio.run(memory.write_message("user", "RAG 召回率从 0.62 提到 0.71"))

    results = asyncio.run(memory.search("RAG 向量化"))

    assert results, "向量检索应命中"
    assert "RAG" in results[0]["content"], "同向向量应排第一"
    # 无关记忆（正交向量）cos 距离=1 → sim=0 < min_similarity → 被过滤
    assert all("RAG" in r["content"] for r in results), "正交记忆应被相似度过滤"


def test_vector_search_fallback_when_no_vectors():
    """无任何向量时走关键词兜底（与 v0.2 行为一致）。"""
    asyncio.run(memory.write_message("user", "记录一次无向量场景"))
    results = asyncio.run(memory.search("向量场景"))
    assert len(results) >= 1
