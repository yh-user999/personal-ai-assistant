"""数据库 schema version 与迁移事务测试。"""
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

from app.models import database


def test_schema_version_is_recorded_and_idempotent(tmp_path, monkeypatch):
    db_file = tmp_path / "assistant.db"
    monkeypatch.setattr("app.config.settings.db_path", str(db_file))
    database.reset_connections()
    database.init_db()
    database.reset_connections()
    database.init_db()
    conn = sqlite3.connect(str(db_file))
    try:
        row = conn.execute(
            "SELECT version FROM schema_version WHERE id=1"
        ).fetchone()
        assert row == (database.SCHEMA_VERSION,)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
        database.reset_connections()


def test_closed_cached_connection_is_rebuilt(tmp_path, monkeypatch):
    """调用方关闭缓存连接后，下一次 connect() 不得复用悬空句柄。"""
    db_file = tmp_path / "connection-rebuild.db"
    monkeypatch.setattr("app.config.settings.db_path", str(db_file))
    database.reset_connections()
    database.init_db()

    first = database.connect()
    first.close()
    second = database.connect()
    try:
        assert second is not first
        assert second.execute("SELECT 1").fetchone()[0] == 1
        assert second.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        database.reset_connections()


def test_foreign_key_integrity_failure_is_fail_fast(tmp_path, monkeypatch):
    """已有孤儿外键记录时，初始化必须失败而不能报告就绪。"""
    db_file = tmp_path / "foreign-key-failure.db"
    monkeypatch.setattr("app.config.settings.db_path", str(db_file))
    database.reset_connections()
    database.init_db()
    database.reset_connections()

    raw = sqlite3.connect(str(db_file))
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute(
            "INSERT INTO novel_project_members(project_id, user_id, role, created_at) "
            "VALUES ('missing-project', '10002', 'member', '2026-09-05T00:00:00+00:00')"
        )
        raw.commit()
    finally:
        raw.close()

    database.reset_connections()
    with pytest.raises(sqlite3.DatabaseError, match="foreign_key_check"):
        database.init_db()
    database.reset_connections()


def test_failed_migration_rolls_back_and_raises(tmp_path, monkeypatch):
    db_file = tmp_path / "assistant.db"
    monkeypatch.setattr("app.config.settings.db_path", str(db_file))
    database.reset_connections()
    original = database._MIGRATIONS
    monkeypatch.setattr(
        database,
        "_MIGRATIONS",
        [
            "ALTER TABLE memories ADD COLUMN atomic_probe TEXT",
            "ALTER TABLE missing_table ADD COLUMN should_fail TEXT",
        ],
    )
    with pytest.raises(sqlite3.OperationalError):
        database.init_db()
    database.reset_connections()
    conn = sqlite3.connect(str(db_file))
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
        assert "atomic_probe" not in columns
        assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0
    finally:
        conn.close()
        database._MIGRATIONS = original
        database.reset_connections()


def test_legacy_user_domain_migration_preserves_report_stats_and_is_idempotent(tmp_path, monkeypatch):
    """旧用户台账/报表回填主人，周报 stats 保留，重复初始化不重复迁移。"""
    db_file = tmp_path / "legacy-user-domain.db"
    conn = sqlite3.connect(str(db_file))
    conn.executescript(
        """
        CREATE TABLE weekly_reports (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          week TEXT NOT NULL UNIQUE,
          content TEXT NOT NULL,
          stats TEXT DEFAULT '{}',
          created_at TEXT NOT NULL
        );
        CREATE TABLE daily_summaries (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          date TEXT NOT NULL UNIQUE,
          content TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE work_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          date TEXT NOT NULL,
          time_range TEXT DEFAULT '',
          content TEXT NOT NULL,
          project TEXT DEFAULT '',
          created_at TEXT NOT NULL
        );
        CREATE TABLE reminders (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          content TEXT NOT NULL,
          remind_at TEXT NOT NULL,
          status TEXT DEFAULT 'pending',
          created_at TEXT NOT NULL
        );
        CREATE TABLE mood_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          mood TEXT NOT NULL,
          snippet TEXT DEFAULT '',
          created_at TEXT NOT NULL
        );
        CREATE TABLE lessons (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          content TEXT NOT NULL UNIQUE,
          context TEXT DEFAULT '',
          created_at TEXT NOT NULL
        );
        CREATE TABLE writing_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          chapter TEXT,
          words INTEGER NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE fitness_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT NOT NULL,
          value REAL,
          detail TEXT DEFAULT '',
          created_at TEXT NOT NULL
        );
        CREATE TABLE initiative_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT NOT NULL,
          content TEXT NOT NULL,
          topic TEXT DEFAULT '',
          sent_at TEXT NOT NULL,
          responded INTEGER DEFAULT 0
        );
        INSERT INTO weekly_reports(week, content, stats, created_at)
          VALUES ('2026-W35', '旧周报内容', '{"messages": 3}', '2026-08-30T12:00:00+00:00');
        INSERT INTO daily_summaries(date, content, created_at)
          VALUES ('2026-08-30', '旧日报内容', '2026-08-30T12:00:00+00:00');
        INSERT INTO work_log(date, content, created_at)
          VALUES ('2026-08-30', '旧工作日志', '2026-08-30T12:00:00+00:00');
        INSERT INTO reminders(content, remind_at, created_at)
          VALUES ('旧提醒', '2026-08-30T13:00:00+00:00', '2026-08-30T12:00:00+00:00');
        INSERT INTO mood_log(mood, snippet, created_at)
          VALUES ('疲惫', '旧情绪', '2026-08-30T12:00:00+00:00');
        INSERT INTO lessons(content, context, created_at)
          VALUES ('旧教训', '旧上下文', '2026-08-30T12:00:00+00:00');
        INSERT INTO writing_log(chapter, words, created_at)
          VALUES ('1', 1000, '2026-08-30T12:00:00+00:00');
        INSERT INTO fitness_log(kind, value, detail, created_at)
          VALUES ('weight', 70.5, '', '2026-08-30T12:00:00+00:00');
        INSERT INTO initiative_log(kind, content, topic, sent_at)
          VALUES ('daily', '旧主动消息', '', '2026-08-30T12:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr("app.config.settings.db_path", str(db_file))
    database.reset_connections()
    database.init_db()

    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    try:
        for table in (
            "work_log",
            "reminders",
            "mood_log",
            "lessons",
            "writing_log",
            "fitness_log",
            "initiative_log",
            "daily_summaries",
            "weekly_reports",
        ):
            assert "user_id" in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id='owner'").fetchone()[0] == 1
        report = conn.execute(
            "SELECT user_id, content, stats FROM weekly_reports WHERE week='2026-W35'"
        ).fetchone()
        assert dict(report) == {
            "user_id": "owner",
            "content": "旧周报内容",
            "stats": '{"messages": 3}',
        }
        lesson = conn.execute(
            "SELECT user_id, content, kind FROM lessons WHERE content='旧教训'"
        ).fetchone()
        assert lesson["user_id"] == "owner"
        from app.services.self_reflect import classify_lesson
        assert lesson["kind"] == classify_lesson("旧教训")
    finally:
        conn.close()

    database.reset_connections()
    database.init_db()
    conn = sqlite3.connect(str(db_file))
    try:
        assert conn.execute("SELECT COUNT(*) FROM weekly_reports").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM work_log").fetchone()[0] == 1
    finally:
        conn.close()
        database.reset_connections()
