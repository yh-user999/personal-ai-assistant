"""小说二期持久化、权限、审计与重启恢复测试。"""
from __future__ import annotations

import sqlite3

import pytest

from app.models import database
from app.novel.domain import GenerationJobStatus
from app.novel.index import rebuild_chapter_index
from app.novel.repository import (
    NovelProjectDeleteError,
    NovelProjectProtectedError,
    SQLiteNovelRepository,
)
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


def test_project_create_rolls_back_new_root_when_database_insert_fails(db, monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.settings.novel_root", str(tmp_path / "novels"))
    repo = SQLiteNovelRepository()
    repo.create_project("已有项目", project_id="existing", slug="same-slug")

    with pytest.raises(sqlite3.IntegrityError):
        repo.create_project("重复项目", project_id="duplicate", slug="same-slug")

    assert not (tmp_path / "novels" / "duplicate").exists()
    with database.db_connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM novel_projects WHERE project_id=?", ("duplicate",)
        ).fetchone() is None


def test_project_create_keeps_existing_root_when_database_insert_fails(db, monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.settings.novel_root", str(tmp_path / "novels"))
    shared_root = tmp_path / "novels" / "shared"
    shared_root.mkdir(parents=True)
    repo = SQLiteNovelRepository()
    repo.create_project("已有项目", project_id="existing", slug="shared-slug", root=str(shared_root))

    with pytest.raises(sqlite3.IntegrityError):
        repo.create_project("重复项目", project_id="duplicate", slug="shared-slug", root=str(shared_root))

    assert shared_root.is_dir()


def test_project_rename_preserves_slug_root_and_increments_version(db, monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.settings.novel_root", str(tmp_path / "novels"))
    root = tmp_path / "novels" / "rename-book"
    repo = SQLiteNovelRepository(owner_id="owner")
    project = repo.create_project("旧书名", project_id="rename", slug="rename-book", root=str(root))

    renamed = repo.update_project("rename", name="新书名", expected_version=project.version)

    assert renamed.name == "新书名"
    assert renamed.slug == "rename-book"
    assert renamed.root == str(root.resolve())
    assert renamed.version == project.version + 1
    assert root.is_dir()


def test_project_delete_cascades_data_and_removes_root(db, monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.settings.novel_root", str(tmp_path / "novels"))
    root = tmp_path / "novels" / "delete-book"
    root.mkdir(parents=True)
    (root / "draft.md").write_text("保留在项目目录内的正文", encoding="utf-8")
    repo = SQLiteNovelRepository(owner_id="owner")
    project = repo.create_project("待删除书", project_id="delete", slug="delete-book", root=str(root))
    repo.add_member("delete", "editor", "editor")
    repo.upsert_chapter("delete", "1", title="开端", content="正文", status="published")
    job = repo.create_job("delete", "1", "delete-job")
    rebuild_chapter_index("delete")
    with database.db_connection() as conn:
        conn.execute(
            "INSERT INTO novel_file_index(project_id,relative_path,size_bytes,mtime_ns,sha256,indexed_at) "
            "VALUES(?,?,?,?,?,?)",
            ("delete", "draft.md", 1, 1, "digest", "now"),
        )
        conn.execute(
            "INSERT INTO novel_audit_logs(user_id,project_id,action,target,summary,created_at) "
            "VALUES(?,?,?,?,?,?)",
            ("owner", "delete", "before.delete", "delete", "{}", "now"),
        )

    result = repo.delete_project("delete", expected_version=project.version)

    assert result == {
        "project_id": "delete",
        "deleted": True,
        "files_deleted": True,
        "cleanup_pending": False,
    }
    assert not root.exists()
    assert repo.list_projects("owner") == []
    with database.db_connection() as conn:
        for table in (
            "novel_project_members",
            "novel_chapters",
            "novel_generation_jobs",
            "novel_file_index",
        ):
            assert conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE project_id=?", ("delete",)
            ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM novel_chapters_fts WHERE project_id=?", ("delete",)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM novel_audit_logs WHERE project_id=?", ("delete",)
        ).fetchone()[0] == 1
    assert repo.get_job(job.job_id) is None


def test_project_delete_rejects_default_and_shared_root(db, monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.settings.novel_root", str(tmp_path / "novels"))
    repo = SQLiteNovelRepository(owner_id="owner")
    repo.get_project("default")
    with pytest.raises(NovelProjectProtectedError, match="不能删除"):
        repo.delete_project("default")

    shared_root = tmp_path / "novels" / "shared"
    shared_root.mkdir(parents=True)
    repo.create_project("第一本", project_id="one", slug="one", root=str(shared_root))
    repo.create_project("第二本", project_id="two", slug="two", root=str(shared_root))
    with pytest.raises(NovelProjectDeleteError, match="共享"):
        repo.delete_project("one")
    assert shared_root.is_dir()
    assert repo.get_project("one").name == "第一本"
    assert repo.get_project("two").name == "第二本"


def test_project_delete_version_conflict_keeps_database_and_root(db, monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.settings.novel_root", str(tmp_path / "novels"))
    root = tmp_path / "novels" / "version-book"
    repo = SQLiteNovelRepository()
    repo.create_project("版本书", project_id="version", slug="version-book", root=str(root))

    with pytest.raises(ValueError, match="版本冲突"):
        repo.delete_project("version", expected_version=2)

    assert root.is_dir()
    assert repo.get_project("version").name == "版本书"


def test_project_delete_rejects_symlink_root(db, monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.settings.novel_root", str(tmp_path / "novels"))
    root = tmp_path / "novels" / "real-book"
    link = tmp_path / "novels" / "linked-book"
    root.mkdir(parents=True)
    link.symlink_to(root, target_is_directory=True)
    repo = SQLiteNovelRepository()
    repo.create_project("链接书", project_id="linked", slug="linked-book", root=str(root))
    with database.db_connection() as conn:
        conn.execute("UPDATE novel_projects SET root=? WHERE project_id=?", (str(link), "linked"))

    with pytest.raises(ValueError, match="符号链接"):
        repo.delete_project("linked")

    assert root.is_dir()
    assert link.is_symlink()
    assert repo.get_project("linked").name == "链接书"


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
