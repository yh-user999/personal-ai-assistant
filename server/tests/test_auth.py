"""API 鉴权中间件测试：API_TOKEN 配置后，非白名单请求需 Bearer token。"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")
os.environ.setdefault("DB_PATH", "/tmp/test_auth.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.models.database import init_db  # noqa: E402
from app.config import settings  # noqa: E402

TOKEN = "test-secret-token"


@pytest.fixture(autouse=True)
def fresh_db():
    db_file = Path("/tmp/test_auth.db")
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_file) + suffix).unlink(missing_ok=True)
    init_db()
    yield


def test_unauthorized_requests_rejected(monkeypatch):
    monkeypatch.setattr(settings, "api_token", TOKEN)
    with TestClient(app) as client:
        # 聊天/统计/事件无 token → 401
        assert client.post("/api/chat", json={"message": "hi"}).status_code == 401
        assert client.get("/api/stats/summary").status_code == 401
        assert client.post("/api/events", json={"events": []}).status_code == 401
        # 白名单放行
        assert client.get("/api/health").status_code == 200


def test_authorized_requests_allowed(monkeypatch):
    monkeypatch.setattr(settings, "api_token", TOKEN)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(app) as client:
        r = client.post("/api/events", json={"events": []}, headers=headers)
        assert r.status_code == 200


def test_no_token_configured_means_open(monkeypatch):
    """API_TOKEN 为空 = 不鉴权（Tailscale 内网等已隔离环境）。"""
    monkeypatch.setattr(settings, "api_token", "")
    with TestClient(app) as client:
        assert client.post("/api/events", json={"events": []}).status_code == 200
