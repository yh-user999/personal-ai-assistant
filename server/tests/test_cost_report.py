"""成本画像回归（P4）：LLM 记账 × 决策轨迹聚合。"""
import pytest

from app.config import settings
from app.models.database import init_db, reset_connections
from app.services import request_trace
from app.services.cost_report import cost_report


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    reset_connections()
    init_db()
    yield
    reset_connections()


def test_cost_report_shape(db_env):
    request_trace.record(
        "owner", "命丛有哪些",
        {"domains": ["novel"], "docs": ["小说-寂静杀戮"]},
        "entity", False, [], {"system_total": 1000}, 30,
    )
    request_trace.record(
        "owner", "炼神有哪些境界",
        {"domains": ["novel"], "docs": []},
        "heal", True, ["炼神"], {"system_total": 2000}, 300,
    )
    report = cost_report(days=7)
    assert report["days"] == 7
    assert "calls" in report["llm"]  # 进程级 LLM 记账快照
    assert report["traces"]["total"] == 2
    assert report["traces"]["avg_injection_bytes"] == 1500.0
    paths = {p["path"]: p["n"] for p in report["traces"]["by_path"]}
    assert paths.get("entity") == 1 and paths.get("heal") == 1


def test_cost_report_empty(db_env):
    report = cost_report(days=7)
    assert report["traces"]["total"] == 0
    assert report["traces"]["avg_injection_bytes"] == 0


def test_cost_report_days_clamped(db_env):
    assert cost_report(days=0)["days"] == 1
    assert cost_report(days=999)["days"] == 90


def test_cost_endpoint(db_env):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.get("/api/stats/cost?days=7")
        assert r.status_code == 200
        payload = r.json()
        assert "llm" in payload and "traces" in payload
