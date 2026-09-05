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
