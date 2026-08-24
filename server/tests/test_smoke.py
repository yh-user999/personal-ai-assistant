"""冒烟测试：建库 / 路由注册 / 健康检查。无需真实 API Key。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.models.database import init_db  # noqa: E402


def test_health():
    init_db()
    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_worklog_route():
    init_db()
    with TestClient(app) as client:
        r = client.post("/api/chat", json={"message": "记录：下午2-5点调RAG性能"})
        assert r.status_code == 200
        assert "已记录" in r.json()["reply"]


def test_heartbeat_and_health():
    with TestClient(app) as client:
        r = client.post(
            "/api/heartbeat",
            json={"client": "collector", "channels": {"window": "2025-01-01T00:00:00+00:00"}},
        )
        assert r.status_code == 200
        hb = client.get("/api/health").json()["collector_heartbeat"]
        assert hb["client"] == "collector"
        assert "window" in hb["channels"]
