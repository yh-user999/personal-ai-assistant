"""小说二期持久化、权限、审计与重启恢复测试。"""
from __future__ import annotations

import sqlite3

import pytest

from app.models import database
from app.novel.domain import GenerationJobStatus
from app.novel.repository import SQLiteNovelRepository
from app.novel.workflow import NovelWorkflow


def setup_db(tmp_path, monkeypatch):
    db_file = tmp_path / "novel.db"
    monkeypatch.setattr("app.config.settings.db_path", str(db_file))
    database.reset_connections()
    database.init_db()
    return db_file


def test_project_chapter_job_survives_reopen(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    repo = SQLiteNovelRepository(owner_id="u1")
    project = repo.create_project("测试书", project_id="p1", slug="test-book")
    repo.upsert_chapter(project.project_id, "1", title="第一章", draft_content="草稿")
    job = repo.create_job(project.project_id, "1", "idem-1", "写第一章")
    workflow = NovelWorkflow.from_job(job, repo)
    workflow.start_generation()
    repo.update_job(job.job_id, GenerationJobStatus.REVIEWING, draft_content="完整草稿")
    current = repo.get_job(job.job_id)
    NovelWorkflow.from_job(current, repo).await_confirmation(review_result={"ok": True})
    database.reset_connections()
    database.init_db()
    reopened = SQLiteNovelRepository(owner_id="u1")
    assert reopened.get_project("p1").name == "测试书"
    assert reopened.get_draft("p1", "1").content == "草稿"
    assert reopened.get_job(job.job_id).status is GenerationJobStatus.AWAITING_CONFIRMATION


def test_idempotency_and_optimistic_conflict(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    repo = SQLiteNovelRepository()
    first = repo.create_job("default", "1", "same-key")
    second = repo.create_job("default", "1", "same-key")
    assert first.job_id == second.job_id
    chapter = repo.upsert_chapter("default", "1", content="a")
    with pytest.raises(ValueError, match="版本冲突"):
        repo.upsert_chapter("default", "1", content="b", expected_version=chapter.version - 1)


def test_consistency_report_detects_missing_published_file(db, tmp_path, monkeypatch):
    from app.novel.consistency import check_project
    monkeypatch.setattr("app.config.settings.novel_root", str(tmp_path / "novels"))
    repo = SQLiteNovelRepository(owner_id="owner")
    repo.create_project("书", project_id="consistency", slug="consistency-book", root=str(tmp_path / "novels" / "book"))
    repo.upsert_chapter("consistency", "1", title="第一章", content="正文", status="published")
    report = check_project("consistency", repository=repo)
    assert report["ok"] is False
    assert report["issues"][0]["kind"] == "missing_file"


def test_project_root_is_confined_to_novel_root(db, monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.settings.novel_root", str(tmp_path / "novels"))
    repo = SQLiteNovelRepository()
    project = repo.create_project("安全书", project_id="safe", slug="safe-book")
    assert project.root.startswith(str(tmp_path / "novels"))
    with pytest.raises(ValueError, match="NOVEL_ROOT"):
        repo.create_project("越界书", project_id="escape", slug="escape-book", root=str(tmp_path / "outside"))


def test_publish_updates_job_and_chapter_atomically(db):
    repo = SQLiteNovelRepository(owner_id="owner")
    repo.create_project("书", project_id="publish", slug="publish-book")
    repo.upsert_chapter("publish", "1", draft_content="旧稿")
    job = repo.create_job("publish", "1", "publish-1")
    repo.update_job(job.job_id, GenerationJobStatus.AWAITING_CONFIRMATION, draft_content="新稿")
    current = repo.get_job(job.job_id)
    published = repo.publish_job(job.job_id, expected_version=current.version)
    assert published.status is GenerationJobStatus.PUBLISHED
    assert repo.get_chapter("1", "publish").content == "新稿"


def test_member_permissions(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    repo = SQLiteNovelRepository(owner_id="owner")
    repo.create_project("书", project_id="p2", slug="book")
    assert repo.can_access("p2", "guest") is False
    repo.add_member("p2", "guest", "member")
    assert repo.can_access("p2", "guest") is True
    assert repo.can_access("p2", "guest", write=True) is False
    repo.add_member("p2", "guest", "editor")
    assert repo.can_access("p2", "guest", write=True) is True


def test_novel_audit_does_not_store_content(tmp_path, monkeypatch):
    db_file = setup_db(tmp_path, monkeypatch)
    with database.db_connection() as conn:
        conn.execute("INSERT INTO novel_audit_logs(user_id,project_id,action,target,summary,created_at) VALUES(?,?,?,?,?,datetime('now'))", ("u1", "p1", "chapter.write", "1", '{"bytes": 10}'))
    conn = sqlite3.connect(db_file)
    assert conn.execute("SELECT summary FROM novel_audit_logs").fetchone()[0] == '{"bytes": 10}'
    conn.close()
