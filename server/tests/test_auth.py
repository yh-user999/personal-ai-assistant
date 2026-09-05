"""API 鉴权中间件测试：API_TOKEN 配置后，非白名单请求需 Bearer token。"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models.database import init_db, reset_connections

TOKEN = "test-secret-token"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """独立临时库。必须 monkeypatch settings.db_path——环境变量 DB_PATH 在这里
    已经太晚（settings 是 lru_cache 单例，conftest 收集阶段就实例化了），
    靠它做隔离会让测试静默写进生产库。"""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    reset_connections()  # 长驻连接缓存握着旧库句柄，切库后必须丢弃
    init_db()
    yield
    reset_connections()


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


def test_role_matrix_blocks_remaining_sensitive_apis(monkeypatch):
    monkeypatch.setattr(settings, "api_token", "owner-token")
    monkeypatch.setattr(settings, "owner_api_token", "")
    monkeypatch.setattr(settings, "collector_api_token", "collector-token")
    monkeypatch.setattr(settings, "executor_api_token", "executor-token")
    monkeypatch.setattr(settings, "qq_api_token", "qq-token")
    with TestClient(app) as client:
        qq = {"Authorization": "Bearer qq-token"}
        collector = {"Authorization": "Bearer collector-token"}
        executor = {"Authorization": "Bearer executor-token"}
        owner = {"Authorization": "Bearer owner-token"}

        sensitive = [
            ("/api/knowledge/docs", "get", None),
            ("/api/documents", "get", None),
            ("/api/reports", "get", None),
            ("/api/daily/latest", "get", None),
            ("/api/stats/summary", "get", None),
            ("/api/reminders/due", "get", None),
            ("/api/messages", "get", None),
            ("/api/mood/state", "get", None),
            ("/api/executor/pending", "get", None),
        ]
        for path, method, _ in sensitive:
            assert getattr(client, method)(path, headers=qq).status_code == 403, path

        assert client.get("/api/events", headers=collector).status_code == 404
        assert client.post("/api/events", json={"events": []}, headers=collector).status_code == 200
        assert client.get("/api/executor/pending", headers=executor).status_code in {200, 404}
        assert client.get("/api/stats/summary", headers=owner).status_code == 200


def test_chat_user_id_cannot_cross_owner_boundary(monkeypatch):
    monkeypatch.setattr(settings, "api_token", "owner-token")
    monkeypatch.setattr(settings, "owner_api_token", "")
    monkeypatch.setattr(settings, "qq_api_token", "qq-token")
    monkeypatch.setattr(settings, "qq_admin_id", "123456")
    with TestClient(app) as client:
        qq = {"Authorization": "Bearer qq-token"}
        assert client.post(
            "/api/chat", json={"message": "hi", "user_id": "123456"}, headers=qq
        ).status_code == 403
        assert client.post(
            "/api/chat", json={"message": "hi", "user_id": "owner"}, headers=qq
        ).status_code == 403
