"""QQ 身份签名边界回归。"""
import time

import pytest
from fastapi.testclient import TestClient

from app.auth import sign_qq_identity
from app.config import settings
from app.main import app
from app.models.database import init_db, reset_connections


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "owner_api_token", "")
    monkeypatch.setattr(settings, "internal_api_token", "")
    monkeypatch.setattr(settings, "collector_api_token", "")
    monkeypatch.setattr(settings, "executor_api_token", "")
    monkeypatch.setattr(settings, "qq_api_token", "qq-token")
    monkeypatch.setattr(settings, "qq_identity_secret", "identity-secret")
    monkeypatch.setattr(settings, "qq_identity_max_age_seconds", 300)
    monkeypatch.setattr(settings, "qq_admin_id", "999")
    reset_connections()
    init_db()
    yield
    reset_connections()


def _headers(user_id: str, request_id: str, timestamp: int | None = None) -> dict[str, str]:
    timestamp = int(time.time()) if timestamp is None else timestamp
    return {
        "Authorization": "Bearer qq-token",
        "X-QQ-User-ID": user_id,
        "X-QQ-Timestamp": str(timestamp),
        "X-QQ-Request-ID": request_id,
        "X-QQ-Signature": sign_qq_identity(
            "identity-secret", user_id, timestamp, request_id
        ),
    }


def test_valid_qq_identity_is_accepted(monkeypatch):
    async def fake_chat(messages, **kwargs):
        return "ok"

    monkeypatch.setattr("app.api.chat.llm.chat", fake_chat)
    request_id = "request-valid"
    with TestClient(app) as client:
        response = client.post(
            "/api/chat",
            json={"message": "x" * 2001, "user_id": "123", "request_id": request_id},
            headers=_headers("123", request_id),
        )
    assert response.status_code == 200


def test_qq_identity_requires_signature_and_fresh_timestamp():
    with TestClient(app) as client:
        missing = client.post(
            "/api/chat", json={"message": "hi", "user_id": "123"},
            headers={"Authorization": "Bearer qq-token"},
        )
        expired = client.post(
            "/api/chat",
            json={"message": "hi", "user_id": "123", "request_id": "expired"},
            headers=_headers("123", "expired", int(time.time()) - 301),
        )
    assert missing.status_code == 401
    assert expired.status_code == 401


def test_qq_identity_cannot_be_tampered_or_impersonate_owner():
    request_id = "request-tampered"
    headers = _headers("123", request_id)
    with TestClient(app) as client:
        tampered = client.post(
            "/api/chat",
            json={"message": "hi", "user_id": "456", "request_id": request_id},
            headers=headers,
        )
        owner = client.post(
            "/api/chat",
            json={"message": "hi", "user_id": "999", "request_id": "request-owner"},
            headers=_headers("999", "request-owner"),
        )
    assert tampered.status_code == 403
    assert owner.status_code == 403
