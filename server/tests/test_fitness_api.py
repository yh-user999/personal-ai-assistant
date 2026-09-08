"""结构化健身 API 流程与权限测试。"""
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models.database import reset_connections


def _configure(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "fitness-api.db"))
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "owner_api_token", "")
    monkeypatch.setattr(settings, "internal_api_token", "")
    monkeypatch.setattr(settings, "collector_api_token", "")
    monkeypatch.setattr(settings, "executor_api_token", "")
    monkeypatch.setattr(settings, "qq_api_token", "")
    reset_connections()


def test_fitness_api_end_to_end(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    with TestClient(app) as client:
        exercises = client.get("/api/fitness/exercises", params={"q": "卧推"})
        assert exercises.status_code == 200
        bench = next(item for item in exercises.json()["results"] if item["name"] == "卧推")

        created = client.post(
            "/api/fitness/plans",
            json={
                "name": "API 全身计划",
                "goal": "减脂保肌",
                "days": [{
                    "day_index": 1,
                    "name": "全身 A",
                    "exercises": [{"exercise_id": bench["id"], "sets": 3, "rep_min": 8}],
                }],
            },
        )
        assert created.status_code == 200
        plan = created.json()
        assert plan["days"][0]["exercises"][0]["rep_max"] == 8

        activated = client.post(f"/api/fitness/plans/{plan['id']}/activate")
        assert activated.status_code == 200
        session = client.post(
            "/api/fitness/sessions",
            json={"plan_id": plan["id"], "plan_day_id": plan["days"][0]["id"]},
        )
        assert session.status_code == 200
        session_id = session.json()["id"]
        logged = client.post(
            f"/api/fitness/sessions/{session_id}/sets",
            json={"exercise_id": bench["id"], "reps": 8, "weight_kg": 60},
        )
        assert logged.status_code == 200
        assert logged.json()["exercise_name"] == "卧推"
        completed = client.post(f"/api/fitness/sessions/{session_id}/complete")
        assert completed.status_code == 200
        assert completed.json()["status"] == "completed"

        summary = client.get("/api/fitness/summary").json()
        assert summary["sessions"]["completed"] == 1
        assert summary["volume_kg"] == 480

        measurement = client.post(
            "/api/fitness/measurements",
            json={"kind": "weight", "value": 70.5},
        )
        assert measurement.status_code == 200
        assert client.get("/api/fitness/measurements").json()["results"][0]["value"] == 70.5


def test_fitness_generate_is_draft_only(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)

    async def fake_generate(*args, **kwargs):
        return {"draft": {"name": "草案", "days": []}, "candidate_count": 8}

    from app.api import fitness as fitness_api
    monkeypatch.setattr(fitness_api.fitness_coach, "generate_plan_draft", fake_generate)
    with TestClient(app) as client:
        response = client.post("/api/fitness/plans/generate", json={"goal": "增肌"})
        assert response.status_code == 200
        assert response.json()["draft"]["name"] == "草案"
        assert client.get("/api/fitness/plans").json()["results"] == []


def test_fitness_api_rejects_collector_when_tokenized(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "owner_api_token", "owner-test-token")
    monkeypatch.setattr(settings, "collector_api_token", "collector-test-token")
    reset_connections()
    with TestClient(app) as client:
        denied = client.get(
            "/api/fitness/summary",
            headers={"Authorization": "Bearer collector-test-token"},
        )
        assert denied.status_code == 403
        allowed = client.get(
            "/api/fitness/summary",
            headers={"Authorization": "Bearer owner-test-token"},
        )
        assert allowed.status_code == 200
