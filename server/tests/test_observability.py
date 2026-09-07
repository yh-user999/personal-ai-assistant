"""阶段 1 Trace schema、脱敏与只读观测接口回归。"""
import json

import pytest

from app.config import settings
from app.models.database import connect, init_db, reset_connections
from app.services import observability, request_trace


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "observability.db"))
    monkeypatch.setattr(settings, "api_token", "")
    reset_connections()
    init_db()
    yield
    reset_connections()


def test_trace_schema_and_metadata_are_redacted(db_env):
    assert request_trace.record(
        "owner",
        "联系 me@example.com，api_key=sk-demo-not-real",
        {"domains": ["novel"], "docs": ["demo-doc"], "prompt": "完整提示词不应落库"},
        "entity",
        True,
        ["demo-term"],
        {"system_total": 120, "prompt": "完整提示词不应落库"},
        18,
        trace_id="trace-demo-1",
        request_id="request-demo-1",
        channel="chat",
        route_name="/api/chat",
        retrieval={"memory_candidates": 3, "memories_used": 2, "prompt": "不应保存"},
        stages={"llm": {"status": "ok", "elapsed_ms": 7, "prompt": "不应保存"}},
        total_latency_ms=42,
    )

    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM request_traces WHERE trace_id=?", ("trace-demo-1",)
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["request_id"] == "request-demo-1"
    assert row["channel"] == "chat"
    assert row["route_name"] == "/api/chat"
    assert row["total_latency_ms"] == 42
    assert "me@example.com" not in row["query"]
    assert "完整提示词不应落库" not in row["routing"]
    assert "prompt" not in json.loads(row["retrieval"])
    assert "prompt" not in json.loads(row["injection_bytes"])
    assert "prompt" not in json.loads(row["stages"])


def test_observability_queries_are_paginated_and_isolated(db_env):
    request_trace.record(
        "owner", "主人问题", {"domains": ["novel"]}, "hybrid", False, [],
        {"system_total": 20}, 5, trace_id="trace-owner", request_id="req-owner",
        channel="chat", total_latency_ms=20,
    )
    request_trace.record(
        "10002", "访客问题", {}, "hybrid", False, [],
        {"system_total": 20}, 5, trace_id="trace-guest", request_id="req-guest",
        channel="qq", total_latency_ms=30,
    )

    page = observability.list_traces("owner", days=7, limit=1, offset=0)
    assert page["total"] == 1
    assert len(page["items"]) == 1
    assert page["items"][0]["trace_id"] == "trace-owner"

    detail = observability.get_trace("owner", "trace-owner")
    assert detail is not None
    assert observability.get_trace("owner", "trace-guest") is None

    summary = observability.summary("owner", days=7)
    assert summary["requests"] == 1
    assert summary["failure_rate"] == 0
    assert summary["by_path"][0]["path"] == "hybrid"


def test_observability_api_is_owner_only(db_env, monkeypatch):
    request_trace.record(
        "owner", "可观测接口测试", {}, "skip", False, [],
        {"system_total": 10}, 1, trace_id="trace-api", request_id="req-api",
    )
    monkeypatch.setattr(settings, "api_token", "owner-token")
    monkeypatch.setattr(settings, "qq_api_token", "qq-token")

    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        unauthorized = client.get("/api/observability/traces?limit=10")
        assert unauthorized.status_code == 401

        response = client.get(
            "/api/observability/traces?limit=10",
            headers={"Authorization": "Bearer owner-token"},
        )
        assert response.status_code == 200
        assert response.json()["items"][0]["trace_id"] == "trace-api"

        denied = client.get(
            "/api/observability/traces/trace-api",
            headers={"Authorization": "Bearer qq-token"},
        )
        assert denied.status_code == 403
