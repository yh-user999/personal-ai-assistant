"""每日 AI 资讯日报 API 权限与响应回归。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.ai_news import api
from app.config import settings
from app.main import app


def test_ai_news_owner_can_read_latest(monkeypatch, db):
    monkeypatch.setattr(settings, "api_token", "owner-token")
    monkeypatch.setattr(settings, "owner_api_token", "")
    monkeypatch.setattr(settings, "qq_api_token", "")

    with TestClient(app) as client:
        response = client.get("/api/ai-news/latest", headers={"Authorization": "Bearer owner-token"})

    assert response.status_code == 200
    assert response.json()["exists"] is False


def test_ai_news_rejects_qq_role(monkeypatch, db):
    monkeypatch.setattr(settings, "api_token", "owner-token")
    monkeypatch.setattr(settings, "qq_api_token", "qq-token")

    with TestClient(app) as client:
        response = client.get("/api/ai-news/latest", headers={"Authorization": "Bearer qq-token"})

    assert response.status_code == 403


def test_ai_news_generate_passes_request_fields(monkeypatch, db):
    monkeypatch.setattr(settings, "api_token", "owner-token")
    monkeypatch.setattr(settings, "owner_api_token", "")
    monkeypatch.setattr(settings, "qq_api_token", "")
    captured = {}

    async def fake_generate(**kwargs):
        captured.update(kwargs)
        return {"status": "ready", "date": "2026-09-18", "content": "日报"}

    monkeypatch.setattr(api.service, "run_daily_ai_news_digest", fake_generate)

    with TestClient(app) as client:
        response = client.post(
            "/api/ai-news/generate",
            headers={"Authorization": "Bearer owner-token", "x-request-id": "req-news"},
            json={"force": True},
        )

    assert response.status_code == 200
    assert response.json()["content"] == "日报"
    assert captured == {"user_id": "owner", "request_id": "req-news", "force": True}
