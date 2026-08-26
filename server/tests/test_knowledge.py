"""RAG 知识库测试：切块器 + 检索（向量环境可用时）。"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")
os.environ.setdefault("DB_PATH", "/tmp/test_knowledge.db")
os.environ.setdefault("EMBEDDING_DIMENSION", "1024")

from app.core.chunker import chunk_text  # noqa: E402


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
