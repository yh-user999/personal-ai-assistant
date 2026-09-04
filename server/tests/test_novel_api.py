"""小说 API 端到端流程测试。"""
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models.database import reset_connections


def test_novel_api_error_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "errors.db"))
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "owner_api_token", "")
    monkeypatch.setattr(settings, "internal_api_token", "")
    reset_connections()
    with TestClient(app) as client:
        response = client.get("/api/novel/projects/missing/chapters")
        assert response.status_code == 404
        assert response.json()["detail"] == {"code": "project_not_found", "message": "项目不存在"}


def test_novel_http_workflow(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "api.db"))
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "owner_api_token", "")
    monkeypatch.setattr(settings, "internal_api_token", "")
    reset_connections()
    with TestClient(app) as client:
        project = client.post("/api/novel/projects", json={"name": "HTTP 小说", "slug": "http-book"})
        assert project.status_code == 200
        project_id = project.json()["project_id"]

        updated = client.patch(
            f"/api/novel/projects/{project_id}",
            json={"name": "HTTP 小说 2", "expected_version": 1},
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "HTTP 小说 2"

        member = client.put(
            f"/api/novel/projects/{project_id}/members",
            json={"user_id": "guest", "role": "member"},
        )
        assert member.status_code == 200
        assert client.get(f"/api/novel/projects/{project_id}/members").json()["members"][0]["user_id"] == "guest"

        chapter = client.put(
            f"/api/novel/projects/{project_id}/chapters",
            json={"chapter_no": "1", "title": "开端", "draft_content": "草稿"},
        )
        assert chapter.status_code == 200

        job = client.post(
            f"/api/novel/projects/{project_id}/jobs",
            json={"chapter_no": "1", "idempotency_key": "http-job", "draft_content": "完成稿"},
        )
        assert job.status_code == 200
        job_id = job.json()["job_id"]

        reviewed = client.post(
            f"/api/novel/projects/{project_id}/jobs/{job_id}/review",
            json={"ok": True, "reply": "通过"},
        )
        assert reviewed.status_code == 200

        published = client.post(f"/api/novel/projects/{project_id}/jobs/{job_id}/confirm")
        assert published.status_code == 200
        assert published.json()["status"] == "published"

        fetched = client.get(f"/api/novel/projects/{project_id}/chapters/1")
        assert fetched.status_code == 200
        assert fetched.json()["content"] == "完成稿"
