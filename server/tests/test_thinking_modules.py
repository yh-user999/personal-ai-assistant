"""思维模块测试：关切追踪 / 术语学习 / 风格学习。"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")
os.environ.setdefault("DB_PATH", "/tmp/test_thinking.db")

from app.models.database import init_db, reset_connections  # noqa: E402
from app.services.concern_tracker import (  # noqa: E402
    get_concerns_injection,
    get_stale_concerns,
    upsert_concerns,
)
from app.services.few_shot import (  # noqa: E402
    detect_positive_feedback,
    get_examples_injection,
    save_example,
)
from app.services.jargon import detect_definition, get_jargon_injection, save_term  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    db_file = Path("/tmp/test_thinking.db")
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_file) + suffix).unlink(missing_ok=True)
    reset_connections()  # 长驻连接缓存还握着被删的旧库句柄，必须丢弃
    init_db()
    yield
    reset_connections()


# ── 关切追踪 ──────────────────────────────────────────────

def test_concern_upsert_and_inject():
    upsert_concerns(["RAG调优", "向量数据库"])
    upsert_concerns(["RAG调优"])
    text = get_concerns_injection()
    assert "RAG调优" in text and "向量数据库" in text
    assert "2 次" in text  # RAG 提到 2 次


def test_stale_concerns_requires_old_and_frequent():
    upsert_concerns(["测试话题"])
    upsert_concerns(["测试话题"])
    # 刚提及的不算 stale
    assert get_stale_concerns(days=3) == []
    # 手动改旧（模拟 5 天前）
    from app.models.database import connect
    conn = connect()
    conn.execute(
        "UPDATE concerns SET last_mentioned_at='2026-01-01T00:00:00+00:00' WHERE topic='测试话题'"
    )
    conn.commit()
    conn.close()
    stale = get_stale_concerns(days=3)
    assert any(s["topic"] == "测试话题" for s in stale)


# ── 术语学习 ──────────────────────────────────────────────

def test_detect_definition():
    assert detect_definition("什么是RAG？") == "RAG"
    assert detect_definition("介绍一下重排序") == "重排序"
    assert detect_definition("今天天气怎么样") is None


def test_jargon_save_and_inject():
    save_term("RAG", "检索增强生成，结合检索与生成的方案")
    assert "RAG" in get_jargon_injection("我的RAG项目遇到问题")
    assert get_jargon_injection("今天吃什么") == ""


# ── 风格学习 ──────────────────────────────────────────────

def test_positive_feedback_detection():
    assert detect_positive_feedback("很好，就是这样")
    assert detect_positive_feedback("不错")
    assert not detect_positive_feedback("帮我写个排序算法，要求完整一些")


def test_example_save_and_inject():
    save_example("简洁回答：先给结论，再给两个要点。")
    text = get_examples_injection()
    assert "先给结论" in text
