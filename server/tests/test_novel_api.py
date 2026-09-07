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


def test_novel_create_project_rejects_blank_name(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "blank-name.db"))
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "owner_api_token", "")
    monkeypatch.setattr(settings, "internal_api_token", "")
    reset_connections()
    with TestClient(app) as client:
        response = client.post("/api/novel/projects", json={"name": "   ", "slug": "blank-name"})
        assert response.status_code == 422
        assert response.json()["detail"] == {
            "code": "project_name_required",
            "message": "书名不能为空",
        }


def test_novel_create_project_reports_duplicate_slug(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "duplicate-slug.db"))
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "owner_api_token", "")
    monkeypatch.setattr(settings, "internal_api_token", "")
    reset_connections()
    with TestClient(app) as client:
        first = client.post("/api/novel/projects", json={"name": "第一本书", "slug": "same-slug"})
        assert first.status_code == 200

        duplicate = client.post("/api/novel/projects", json={"name": "第二本书", "slug": "same-slug"})
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"] == {
            "code": "project_slug_conflict",
            "message": "项目标识已存在",
        }


def test_novel_create_project_reports_invalid_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "invalid-root.db"))
    monkeypatch.setattr(settings, "novel_root", str(tmp_path / "novels"))
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "owner_api_token", "")
    monkeypatch.setattr(settings, "internal_api_token", "")
    reset_connections()
    with TestClient(app) as client:
        response = client.post(
            "/api/novel/projects",
            json={"name": "越界书", "slug": "outside-root", "root": str(tmp_path / "outside")},
        )
        assert response.status_code == 422
        assert response.json()["detail"] == {
            "code": "project_root_invalid",
            "message": "小说项目根目录必须位于 NOVEL_ROOT 内",
        }


def test_novel_role_boundary_allows_owner_and_internal_only(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "role-boundary.db"))
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "owner_api_token", "owner-test-token")
    monkeypatch.setattr(settings, "internal_api_token", "internal-test-token")
    monkeypatch.setattr(settings, "collector_api_token", "collector-test-token")
    monkeypatch.setattr(settings, "qq_api_token", "qq-test-token")
    reset_connections()
    with TestClient(app) as client:
        owner = client.post(
            "/api/novel/projects",
            headers={"Authorization": "Bearer owner-test-token"},
            json={"name": "主人项目", "slug": "owner-project"},
        )
        assert owner.status_code == 200

        internal = client.post(
            "/api/novel/projects",
            headers={"Authorization": "Bearer internal-test-token"},
            json={"name": "内部项目", "slug": "internal-project"},
        )
        assert internal.status_code == 200

        for token in ("collector-test-token", "qq-test-token"):
            denied = client.post(
                "/api/novel/projects",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "不应创建", "slug": f"denied-{token}"},
            )
            assert denied.status_code == 403

        unauthenticated = client.post(
            "/api/novel/projects",
            json={"name": "未授权", "slug": "unauthorized"},
        )
        assert unauthenticated.status_code == 401


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

        listed = client.get(f"/api/novel/projects/{project_id}/chapters").json()
        nos = [c["chapter_no"] for c in listed["chapters"]]
        # 回归：list_chapters 曾把 project_id 误当 chapter_no 返回，
        # 工作台章节列表整列显示成项目 UUID
        assert "1" in nos, nos
        assert project_id not in nos, nos
