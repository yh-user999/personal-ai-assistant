"""连接缓存语义回归：close() 归还缓存不真关；事务中/换库路径仍真关。

186 处既有 `conn.close()` 依赖本语义：close 只是把连接归还线程本地缓存，
下一次 connect() 复用同一底层连接（省掉 WAL pragma 与 sqlite-vec 扩展加载）。
"""
import sqlite3

import pytest

from app.config import settings
from app.models.database import connect, reset_connections


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "cache.db"))
    reset_connections()
    yield
    reset_connections()


def _is_open(conn) -> bool:
    try:
        conn.execute("SELECT 1")
        return True
    except sqlite3.ProgrammingError:
        return False


def test_close_returns_connection_to_cache(env):
    conn1 = connect()
    conn1.close()
    conn2 = connect()
    assert conn2 is conn1, "close 后同线程重连应复用缓存连接"
    assert _is_open(conn2)


def test_repeated_connect_close_reuses_same_connection(env):
    seen = []
    for _ in range(5):
        conn = connect()
        conn.execute("SELECT 1")
        conn.close()
        seen.append(conn)
    assert all(conn is seen[0] for conn in seen), "反复 connect/close 不应重建连接"


def test_close_with_open_transaction_force_closes(env):
    conn1 = connect()
    conn1.execute("BEGIN")
    conn1.close()
    assert not _is_open(conn1), "事务中 close 必须真关（隐式回滚），防止半提交状态泄漏"
    conn2 = connect()
    assert conn2 is not conn1


def test_close_mid_transaction_rolls_back(env):
    conn1 = connect()
    conn1.execute("CREATE TABLE IF NOT EXISTS t(x)")
    conn1.execute("INSERT INTO t VALUES (1)")
    conn1.close()
    conn2 = connect()
    assert conn2.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_switch_db_force_closes_old_connection(env, tmp_path, monkeypatch):
    conn1 = connect()
    conn1.execute("CREATE TABLE t(x)")
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "other.db"))
    conn2 = connect()
    assert conn2 is not conn1
    assert not _is_open(conn1), "换库时旧连接必须真关，防句柄泄漏（Windows 文件锁）"
