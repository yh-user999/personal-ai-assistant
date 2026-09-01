"""Phase 1 多人隔离回归：老库迁移 + 记忆层按 user_id 隔离。

背景（v0.4 多人支持）：
- 8 张用户态表（memories/facts/profile/concerns/jargon_terms/
  style_examples/goals/unresolved_issues）加 user_id
- 老库数据回填主人身份；profile/concerns/jargon_terms/facts 需重建表
- memories_fts 加 user_id 列（删旧重建 + 回填）
- 全部读写默认 user_id=None → 主人（测试环境为 'owner'），旧行为不变
"""
import asyncio
import sqlite3
from pathlib import Path

import pytest

from app.core import memory
from app.models.database import init_db, reset_connections


# ── 1. 老库迁移 ─────────────────────────────────────────────

LEGACY_SCHEMA = """
CREATE TABLE memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sender TEXT NOT NULL,
  content TEXT NOT NULL,
  summary TEXT DEFAULT '',
  topics TEXT DEFAULT '[]',
  ts TEXT NOT NULL,
  importance REAL DEFAULT 1.0
);
CREATE TABLE facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject TEXT NOT NULL,
  predicate TEXT NOT NULL,
  object TEXT NOT NULL,
  source_memory_id INTEGER,
  confidence REAL DEFAULT 0.7,
  updated_at TEXT NOT NULL,
  UNIQUE(subject, predicate, object)
);
CREATE TABLE profile (
  dimension TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  confidence REAL DEFAULT 0.5,
  updated_at TEXT NOT NULL
);
CREATE TABLE concerns (
  topic TEXT PRIMARY KEY,
  mention_count INTEGER DEFAULT 1,
  last_mentioned_at TEXT NOT NULL
);
CREATE TABLE jargon_terms (
  term TEXT PRIMARY KEY,
  explanation TEXT NOT NULL,
  created_at TEXT NOT NULL,
  times_used INTEGER DEFAULT 0
);
CREATE TABLE style_examples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE goals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  status TEXT DEFAULT 'active',
  progress TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE unresolved_issues (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic TEXT NOT NULL,
  context TEXT DEFAULT '',
  status TEXT DEFAULT 'open',
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE VIRTUAL TABLE memories_fts USING fts5(memory_id UNINDEXED, grams);
"""


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    """造一个 v0.3 老库（无 user_id），塞数据后跑 init_db 迁移。"""
    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_file))
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO memories (sender, content, ts) VALUES ('user','旧记忆内容甲','2026-08-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO facts (subject, predicate, object, updated_at) VALUES ('用户','职业','运维','2026-08-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO profile (dimension, value, updated_at) VALUES ('technical_background','运维','2026-08-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO concerns (topic, last_mentioned_at) VALUES ('RAG调优','2026-08-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO jargon_terms (term, explanation, created_at) VALUES ('RAG','检索增强生成','2026-08-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO style_examples (content, created_at) VALUES ('范例','2026-08-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO goals (title, created_at, updated_at) VALUES ('目标甲','2026-08-01T00:00:00+00:00','2026-08-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO unresolved_issues (topic, created_at) VALUES ('未解决甲','2026-08-01T00:00:00+00:00')"
    )
    conn.execute("INSERT INTO memories_fts (memory_id, grams) VALUES (1, '旧记忆内容甲')")
    conn.commit()
    conn.close()

    monkeypatch.setattr("app.config.settings.db_path", str(db_file))
    reset_connections()
    init_db()
    yield db_file
    reset_connections()


def _table_cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_legacy_migration_adds_user_id_and_backfills(legacy_db):
    conn = sqlite3.connect(str(legacy_db))
    conn.row_factory = sqlite3.Row
    # 8 张表都有 user_id 列
    for t in ("memories", "facts", "profile", "concerns", "jargon_terms",
              "style_examples", "goals", "unresolved_issues"):
        assert "user_id" in _table_cols(conn, t), f"{t} 缺 user_id"
    # 老数据全部回填主人（测试环境 qq_admin_id 空 → 'owner'）
    for t, cond in (
        ("memories", "content='旧记忆内容甲'"),
        ("facts", "subject='用户'"),
        ("profile", "dimension='technical_background'"),
        ("concerns", "topic='RAG调优'"),
        ("jargon_terms", "term='RAG'"),
        ("style_examples", "content='范例'"),
        ("goals", "title='目标甲'"),
        ("unresolved_issues", "topic='未解决甲'"),
    ):
        row = conn.execute(f"SELECT user_id FROM {t} WHERE {cond}").fetchone()
        assert row is not None and row["user_id"] == "owner", f"{t} 回填失败"
    # FTS 表重建带 user_id 并回填
    fts_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='memories_fts'"
    ).fetchone()[0]
    assert "user_id" in fts_sql
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM memories_fts WHERE user_id='owner'"
    ).fetchone()["n"] == 1
    conn.close()


def test_legacy_migration_idempotent(legacy_db):
    """再跑一次 init_db 不炸、不回填错（user_id 已有 → 跳过）。"""
    reset_connections()
    init_db()
    conn = sqlite3.connect(str(legacy_db))
    conn.row_factory = sqlite3.Row
    n = conn.execute("SELECT COUNT(*) AS n FROM memories WHERE user_id='owner'").fetchone()["n"]
    assert n == 1
    conn.close()


# ── 2. 记忆读写按用户隔离 ────────────────────────────────────

def test_write_message_scoped_and_dedup_per_user(db):
    mid_a1 = asyncio.run(memory.write_message("user", "主人说A", user_id=None))
    mid_g = asyncio.run(memory.write_message("user", "主人说A", user_id="10002"))
    assert mid_a1 is not None and mid_g is not None  # 不同用户不算重复
    # 同用户重复 → 去重
    assert asyncio.run(memory.write_message("user", "主人说A", user_id="10002")) is None
    # 落库的 user_id 正确
    from app.models.database import connect

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT user_id FROM memories WHERE content='主人说A' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert [r["user_id"] for r in rows] == ["owner", "10002"]


def test_search_and_history_isolated(db):
    asyncio.run(memory.write_message("user", "主人在调 RAG 向量库性能", user_id=None))
    asyncio.run(memory.write_message("user", "访客在写小说提纲", user_id="10002"))
    asyncio.run(memory.write_message("assistant", "好的，帮你梳理", user_id="10002"))

    # 主人检索只看到自己的
    owner_hits = asyncio.run(memory.search("RAG 向量", top_k=5, user_id=None))
    assert owner_hits and all("RAG" in h.get("content", "") for h in owner_hits)
    # 访客检索只看到自己的：断言隔离性（不含主人内容）而非"每条都含关键词"——
    # 配了真实 embedding key 时，同会话的"好的，帮你梳理"会被语义通道正常召回
    # （相似度 0.55 > min_similarity），那是检索生效的表现，不是串味。
    guest_hits = asyncio.run(memory.search("小说提纲", top_k=5, user_id="10002"))
    assert guest_hits
    assert any("小说" in h.get("content", "") for h in guest_hits)
    assert not any("RAG" in h.get("content", "") for h in guest_hits)
    # 主人检索不到访客内容（隔离铁律）。注意不能断言"结果为空"——有 embedding
    # key 时主人自己的消息会被弱相似度召回，那是自己的数据，不违反隔离。
    owner_novel = asyncio.run(memory.search("小说提纲", top_k=5, user_id=None))
    assert not any("访客" in h.get("content", "") for h in owner_novel)
    assert not any("梳理" in h.get("content", "") for h in owner_novel)

    # 多轮历史隔离
    guest_hist = memory.get_recent_history(10, user_id="10002")
    assert [h["role"] for h in guest_hist] == ["user", "assistant"]
    assert all("小说" in h["content"] or "梳理" in h["content"] for h in guest_hist)
    owner_hist = memory.get_recent_history(10, user_id=None)
    assert all("小说" not in h["content"] for h in owner_hist)


def test_fts_query_scoped(db):
    mid = asyncio.run(memory.write_message("user", "独角兽专有名词测试", user_id="10002"))
    hits_guest = memory._fts_query("独角兽专有", 5, user_id="10002")
    assert any(h["id"] == mid for h in hits_guest)
    assert memory._fts_query("独角兽专有", 5, user_id=None) == []
    # 主人关键词命中不到访客记忆
    owner_mid = asyncio.run(memory.write_message("user", "主人自己的关键词", user_id=None))
    assert any(h["id"] == owner_mid for h in memory._fts_query("主人自己", 5, user_id=None))


def test_facts_isolated(db):
    from app.services.fact_extract import upsert_facts

    upsert_facts([{"subject": "用户", "predicate": "职业", "object": "运维"}], user_id=None)
    upsert_facts([{"subject": "访客", "predicate": "职业", "object": "学生"}], user_id="10002")
    owner_facts = memory.get_facts_injection(user_id=None)
    guest_facts = memory.get_facts_injection(user_id="10002")
    assert "运维" in owner_facts and "学生" not in owner_facts
    assert "学生" in guest_facts and "运维" not in guest_facts


def test_normalize_user_id():
    assert memory.normalize_user_id(None) == "owner"
    assert memory.normalize_user_id("") == "owner"
    assert memory.normalize_user_id("10002") == "10002"
    with pytest.raises(ValueError):
        memory.normalize_user_id("abc")
    with pytest.raises(ValueError):
        memory.normalize_user_id("1234567890123")  # 13 位超长
    with pytest.raises(ValueError):
        memory.normalize_user_id("1.5")
