"""冒烟测试：建库 / 路由注册 / 健康检查。无需真实 API Key。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import NOVEL_WORKBENCH_VERSION, app
from app.models.database import init_db, reset_connections


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """独立临时库：本文件此前完全没做隔离，init_db 与"记录：…"写入
    都落在生产库 ./data/assistant.db 上。"""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    reset_connections()
    init_db()
    yield
    reset_connections()


def test_health():
    init_db()
    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        payload = r.json()
        assert payload == {
            "status": "ok",
            "version": app.version,
            "workbench_version": NOVEL_WORKBENCH_VERSION,
        }
        assert "collector_heartbeat" not in payload
        assert "timestamp" not in payload
        assert "app_name" not in payload


def test_ready_reports_dependency_state():
    with TestClient(app) as client:
        health = client.get("/api/health")
        ready = client.get("/api/ready")
    assert health.status_code == 200
    assert ready.status_code in {200, 503}
    payload = ready.json()
    assert payload["status"] in {"ready", "not_ready"}
    assert {"database", "scheduler", "llm", "vector"} <= set(payload["checks"])


def test_worklog_route():
    init_db()
    with TestClient(app) as client:
        r = client.post("/api/chat", json={"message": "记录：下午2-5点调RAG性能"})
        assert r.status_code == 200
        assert "已记录" in r.json()["reply"]


def test_ready_allows_keyword_fallback_when_vectors_are_unavailable(monkeypatch):
    from app.models import database

    with TestClient(app) as client:
        # 首次 ready 会在线程池线程建立连接并探测扩展，先预热后再注入降级状态。
        client.get("/api/ready")
        monkeypatch.setattr(database, "_vec_state", False)
        response = client.get("/api/ready")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["checks"]["database"]["status"] == "ok"
    assert payload["checks"]["vector"] == {
        "status": "degraded",
        "ok": False,
        "available": False,
        "fallback": "keyword",
    }
    assert payload["degraded"] == ["vector"]
    assert payload["failures"] == []


def test_ready_reports_integrity_reasons_when_check_fails(monkeypatch):
    """integrity_check 返回多行错误时必须判失败并带上可诊断原因。

    旧实现只取首行，既无法排障，也会在首行为提示文本时误判。
    """
    from app.models import database

    real_connect = database.connect
    failure_rows = [
        ("*** in database main ***",),
        ("Page 42 is never used",),
    ]

    class _FakeCursor:
        def __init__(self, rows):
            self._rows = list(rows)

        def fetchall(self):
            return list(self._rows)

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def __iter__(self):
            return iter(self._rows)

    class _FakeConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args):
            if sql.strip().upper().startswith("PRAGMA INTEGRITY_CHECK"):
                return _FakeCursor(failure_rows)
            return self._inner.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def fake_connect():
        return _FakeConn(real_connect())

    with TestClient(app) as client:
        client.get("/api/ready")
        monkeypatch.setattr(database, "connect", fake_connect)
        response = client.get("/api/ready")

    assert response.status_code == 503
    db_check = response.json()["checks"]["database"]
    assert db_check["ok"] is False
    assert db_check["integrity"] is False
    assert db_check["integrity_errors"] == [
        "*** in database main ***",
        "Page 42 is never used",
    ]


def test_heartbeat_does_not_expose_activity_on_health():
    with TestClient(app) as client:
        r = client.post(
            "/api/heartbeat",
            json={"client": "collector", "channels": {"window": "2025-01-01T00:00:00+00:00"}},
        )
        assert r.status_code == 200
        payload = client.get("/api/health").json()
        assert payload["status"] == "ok"
        assert "collector_heartbeat" not in payload
        assert "timestamp" not in payload
