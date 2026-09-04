"""小说项目、章节和生成任务的 SQLite 持久化仓储。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from app.config import settings
from app.models.database import db_connection
from app.novel.domain import Chapter, GenerationJob, GenerationJobStatus, NovelDraft, NovelProject, can_transition_job


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class NovelRepository(Protocol):
    def get_project(self, project_id: str | None = None) -> NovelProject: ...
    def get_chapter(self, chapter_no: str, project_id: str | None = None) -> Chapter | None: ...
    def list_chapters(self, project_id: str | None = None) -> list[Chapter]: ...


class SQLiteNovelRepository:
    """支持幂等、乐观锁和重启恢复的小说仓储。"""

    def __init__(self, *, owner_id: str = "owner") -> None:
        self.owner_id = owner_id or "owner"

    def ensure_default_project(self) -> NovelProject:
        with db_connection() as conn:
            row = conn.execute("SELECT * FROM novel_projects WHERE project_id='default'").fetchone()
            if row:
                return self._project(row)
            now = _now()
            root = self._project_root(None, "default")
            conn.execute("INSERT INTO novel_projects(project_id, owner_id, name, slug, root, created_at, updated_at) VALUES ('default', ?, '默认小说', 'default', ?, ?, ?)", (self.owner_id, root, now, now))
            return NovelProject("default", "默认小说", "default", self.owner_id, root=root, version=1, updated_at=now)

    def create_project(self, name: str, *, project_id: str | None = None, slug: str | None = None, owner_id: str | None = None, root: str | None = None, metadata: dict | None = None) -> NovelProject:
        project_id = project_id or str(uuid.uuid4())
        slug = slug or project_id
        owner_id = owner_id or self.owner_id
        root = self._project_root(root, project_id)
        now = _now()
        with db_connection() as conn:
            conn.execute("INSERT INTO novel_projects(project_id, owner_id, name, slug, root, metadata, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (project_id, owner_id, name, slug, root, json.dumps(metadata or {}, ensure_ascii=False), now, now))
        return NovelProject(project_id, name, slug, owner_id, root, metadata or {}, 1, now)

    def get_project(self, project_id: str | None = None) -> NovelProject:
        project_id = project_id or "default"
        with db_connection() as conn:
            row = conn.execute("SELECT * FROM novel_projects WHERE project_id=?", (project_id,)).fetchone()
        if not row:
            if project_id == "default":
                return self.ensure_default_project()
            raise KeyError(f"项目不存在: {project_id}")
        return self._project(row)

    def update_project(self, project_id: str, *, name: str | None = None, root: str | None = None, metadata: dict | None = None, expected_version: int | None = None) -> NovelProject:
        with db_connection() as conn:
            row = conn.execute("SELECT * FROM novel_projects WHERE project_id=?", (project_id,)).fetchone()
            if not row:
                raise KeyError(f"项目不存在: {project_id}")
            if expected_version is not None and row["version"] != expected_version:
                raise ValueError("项目版本冲突")
            next_root = self._project_root(root, project_id) if root is not None else row["root"]
            now = _now()
            conn.execute("UPDATE novel_projects SET name=COALESCE(?,name), root=?, metadata=COALESCE(?,metadata), version=version+1, updated_at=? WHERE project_id=?", (name, next_root, json.dumps(metadata, ensure_ascii=False) if metadata is not None else None, now, project_id))
            row = conn.execute("SELECT * FROM novel_projects WHERE project_id=?", (project_id,)).fetchone()
        return self._project(row)

    def list_projects(self, user_id: str | None = None) -> list[NovelProject]:
        user_id = user_id or self.owner_id
        with db_connection() as conn:
            rows = conn.execute("SELECT p.* FROM novel_projects p LEFT JOIN novel_project_members m ON m.project_id=p.project_id WHERE p.owner_id=? OR m.user_id=? ORDER BY p.updated_at DESC", (user_id, user_id)).fetchall()
        return [self._project(row) for row in rows]

    @staticmethod
    def _project_root(root: str | None, project_id: str) -> str:
        base = Path(settings.novel_root).expanduser().resolve()
        candidate = (Path(root).expanduser() if root else base / project_id).resolve()
        if candidate != base and base not in candidate.parents:
            raise ValueError("小说项目根目录必须位于 NOVEL_ROOT 内")
        candidate.mkdir(parents=True, exist_ok=True)
        return str(candidate)

    def add_member(self, project_id: str, user_id: str, role: str = "member") -> None:
        with db_connection() as conn:
            owner = conn.execute("SELECT owner_id FROM novel_projects WHERE project_id=?", (project_id,)).fetchone()
            if not owner:
                raise KeyError(f"项目不存在: {project_id}")
            if owner["owner_id"] == user_id and role != "owner":
                raise ValueError("项目所有者不能降级")
            conn.execute("INSERT INTO novel_project_members(project_id,user_id,role,created_at) VALUES(?,?,?,?) ON CONFLICT(project_id,user_id) DO UPDATE SET role=excluded.role", (project_id, user_id, role, _now()))

    def remove_member(self, project_id: str, user_id: str) -> None:
        with db_connection() as conn:
            owner = conn.execute("SELECT owner_id FROM novel_projects WHERE project_id=?", (project_id,)).fetchone()
            if not owner:
                raise KeyError(f"项目不存在: {project_id}")
            if owner["owner_id"] == user_id:
                raise ValueError("项目所有者不能移除")
            conn.execute("DELETE FROM novel_project_members WHERE project_id=? AND user_id=?", (project_id, user_id))

    def can_access(self, project_id: str, user_id: str, *, write: bool = False) -> bool:
        with db_connection() as conn:
            row = conn.execute("SELECT owner_id FROM novel_projects WHERE project_id=?", (project_id,)).fetchone()
            if not row:
                return False
            if row["owner_id"] == user_id:
                return True
            member = conn.execute("SELECT role FROM novel_project_members WHERE project_id=? AND user_id=?", (project_id, user_id)).fetchone()
        return bool(member and (not write or member["role"] in {"owner", "editor"}))

    def upsert_chapter(self, project_id: str, chapter_no: str, *, title: str = "", content: str | None = None, draft_content: str | None = None, status: str = "draft", expected_version: int | None = None) -> NovelDraft:
        now = _now()
        with db_connection() as conn:
            row = conn.execute("SELECT * FROM novel_chapters WHERE project_id=? AND chapter_no=?", (project_id, chapter_no)).fetchone()
            if row:
                if expected_version is not None and row["version"] != expected_version:
                    raise ValueError("章节版本冲突")
                next_version = row["version"] + 1
                conn.execute("UPDATE novel_chapters SET title=?, content=COALESCE(?,content), draft_content=COALESCE(?,draft_content), status=?, version=?, updated_at=? WHERE project_id=? AND chapter_no=?", (title, content, draft_content, status, next_version, now, project_id, chapter_no))
            else:
                next_version = 1
                conn.execute("INSERT INTO novel_chapters(project_id,chapter_no,title,content,draft_content,status,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (project_id, chapter_no, title, content or "", draft_content or "", status, next_version, now, now))
            value = conn.execute("SELECT * FROM novel_chapters WHERE project_id=? AND chapter_no=?", (project_id, chapter_no)).fetchone()
        return NovelDraft(project_id, chapter_no, value["draft_content"], value["version"], value["status"])

    def get_draft(self, project_id: str, chapter_no: str) -> NovelDraft | None:
        with db_connection() as conn:
            row = conn.execute("SELECT * FROM novel_chapters WHERE project_id=? AND chapter_no=?", (project_id, chapter_no)).fetchone()
        return self._draft(row) if row else None

    def get_chapter(self, chapter_no: str, project_id: str | None = None) -> Chapter | None:
        project_id = project_id or "default"
        with db_connection() as conn:
            row = conn.execute("SELECT * FROM novel_chapters WHERE project_id=? AND chapter_no=?", (project_id, chapter_no)).fetchone()
        if row:
            return Chapter(row["chapter_no"], row["title"], row["content"] or row["draft_content"], project_id, row["status"])
        if project_id == "default":
            return LegacyNovelRepository().get_chapter(chapter_no, project_id)
        return None

    def list_chapters(self, project_id: str | None = None) -> list[Chapter]:
        project_id = project_id or "default"
        with db_connection() as conn:
            rows = conn.execute("SELECT * FROM novel_chapters WHERE project_id=? ORDER BY CAST(chapter_no AS INTEGER), chapter_no", (project_id,)).fetchall()
        if rows:
            return [Chapter(project_id, r["title"], r["content"] or r["draft_content"], project_id, r["status"]) for r in rows]
        return LegacyNovelRepository().list_chapters(project_id)

    def create_job(self, project_id: str, chapter_no: str, idempotency_key: str, prompt: str = "") -> GenerationJob:
        now = _now()
        with db_connection() as conn:
            row = conn.execute("SELECT * FROM novel_generation_jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if not row:
                job_id = str(uuid.uuid4())
                conn.execute("INSERT INTO novel_generation_jobs(job_id,idempotency_key,project_id,chapter_no,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (job_id, idempotency_key, project_id, chapter_no, prompt, now, now))
                row = conn.execute("SELECT * FROM novel_generation_jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._job(row)

    def recover_stale_jobs(self, *, timeout_seconds: int = 900) -> int:
        """把心跳超时未完成的 generating 任务重新排队，供重启/定时恢复。"""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
        recovered = 0
        with db_connection() as conn:
            rows = conn.execute("SELECT job_id, updated_at, heartbeat_at FROM novel_generation_jobs WHERE status=?", (GenerationJobStatus.GENERATING.value,)).fetchall()
            for row in rows:
                try:
                    heartbeat = datetime.fromisoformat(row["heartbeat_at"] or row["updated_at"])
                except ValueError:
                    continue
                try:
                    updated_at = datetime.fromisoformat(row["updated_at"])
                except ValueError:
                    updated_at = heartbeat
                if heartbeat < cutoff or updated_at < cutoff:
                    recovered += conn.execute("UPDATE novel_generation_jobs SET status=?, error=?, claimed_by='', heartbeat_at=NULL, version=version+1, updated_at=? WHERE job_id=? AND status=?", (GenerationJobStatus.QUEUED.value, "生成任务超时，已自动重新排队", _now(), row["job_id"], GenerationJobStatus.GENERATING.value)).rowcount
        return recovered

    def claim_next_job(self, *, worker_id: str = "") -> GenerationJob | None:
        """原子认领一个 queued 任务；进程重启后 queued 任务可再次执行。"""
        now = _now()
        with db_connection() as conn:
            row = conn.execute("SELECT * FROM novel_generation_jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            changed = conn.execute("UPDATE novel_generation_jobs SET status=?, attempts=attempts+1, progress=0, claimed_by=?, heartbeat_at=?, version=version+1, updated_at=? WHERE job_id=? AND status='queued'", (GenerationJobStatus.GENERATING.value, worker_id, now, now, row["job_id"])).rowcount
            if not changed:
                return None
            row = conn.execute("SELECT * FROM novel_generation_jobs WHERE job_id=?", (row["job_id"],)).fetchone()
        return self._job(row)

    def update_job(self, job_id: str, status: GenerationJobStatus, *, draft_content: str | None = None, review_result: dict | None = None, error: str = "", progress: int | None = None, heartbeat: bool = False, expected_version: int | None = None) -> GenerationJob:
        now = _now()
        with db_connection() as conn:
            row = conn.execute("SELECT * FROM novel_generation_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(f"任务不存在: {job_id}")
            current = GenerationJobStatus(row["status"])
            if not can_transition_job(current, status):
                raise ValueError(f"非法任务状态迁移: {current.value} -> {status.value}")
            if expected_version is not None and row["version"] != expected_version:
                raise ValueError("任务版本冲突")
            next_progress = max(0, min(100, progress)) if progress is not None else row["progress"]
            heartbeat_at = now if heartbeat or status == GenerationJobStatus.GENERATING else row["heartbeat_at"]
            claimed_by = "" if status in {GenerationJobStatus.QUEUED, GenerationJobStatus.FAILED, GenerationJobStatus.CANCELLED, GenerationJobStatus.PUBLISHED} else row["claimed_by"]
            conn.execute("UPDATE novel_generation_jobs SET status=?, draft_content=COALESCE(?,draft_content), review_result=COALESCE(?,review_result), error=?, progress=?, heartbeat_at=?, claimed_by=?, version=?, updated_at=? WHERE job_id=?", (status.value, draft_content, json.dumps(review_result, ensure_ascii=False) if review_result is not None else None, error, next_progress, heartbeat_at, claimed_by, row["version"] + 1, now, job_id))
            row = conn.execute("SELECT * FROM novel_generation_jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._job(row)

    def heartbeat_job(self, job_id: str, *, progress: int | None = None, expected_version: int | None = None) -> GenerationJob:
        return self.update_job(job_id, GenerationJobStatus.GENERATING, progress=progress, heartbeat=True, expected_version=expected_version)

    def publish_job(self, job_id: str, *, expected_version: int | None = None) -> GenerationJob:
        """在同一事务中发布任务并写入章节正文。"""
        now = _now()
        with db_connection() as conn:
            job = conn.execute("SELECT * FROM novel_generation_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not job:
                raise KeyError(f"任务不存在: {job_id}")
            if job["status"] != GenerationJobStatus.AWAITING_CONFIRMATION.value:
                raise ValueError("只有待确认任务才能发布")
            if expected_version is not None and job["version"] != expected_version:
                raise ValueError("任务版本冲突")
            next_job_version = job["version"] + 1
            conn.execute("UPDATE novel_generation_jobs SET status=?, version=?, updated_at=? WHERE job_id=?", (GenerationJobStatus.PUBLISHED.value, next_job_version, now, job_id))
            chapter = conn.execute("SELECT * FROM novel_chapters WHERE project_id=? AND chapter_no=?", (job["project_id"], job["chapter_no"])).fetchone()
            if chapter:
                conn.execute("UPDATE novel_chapters SET content=?, draft_content=?, status='published', version=version+1, updated_at=? WHERE project_id=? AND chapter_no=?", (job["draft_content"], job["draft_content"], now, job["project_id"], job["chapter_no"]))
            else:
                conn.execute("INSERT INTO novel_chapters(project_id,chapter_no,content,draft_content,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (job["project_id"], job["chapter_no"], job["draft_content"], job["draft_content"], "published", now, now))
            job = conn.execute("SELECT * FROM novel_generation_jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._job(job)

    def retry_job(self, job_id: str, *, max_attempts: int = 3) -> GenerationJob:
        """将失败任务重新排队；超过预算则保持 failed。"""
        with db_connection() as conn:
            row = conn.execute("SELECT * FROM novel_generation_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(f"任务不存在: {job_id}")
            if row["status"] != GenerationJobStatus.FAILED.value:
                raise ValueError("只有失败任务可以重试")
            if row["attempts"] >= max_attempts:
                return self._job(row)
            conn.execute("UPDATE novel_generation_jobs SET status=?, error='', version=version+1, updated_at=? WHERE job_id=? AND status=?", (GenerationJobStatus.QUEUED.value, _now(), job_id, GenerationJobStatus.FAILED.value))
            row = conn.execute("SELECT * FROM novel_generation_jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._job(row)

    def get_job(self, job_id: str) -> GenerationJob | None:
        with db_connection() as conn:
            row = conn.execute("SELECT * FROM novel_generation_jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def list_jobs(self, project_id: str) -> list[GenerationJob]:
        with db_connection() as conn:
            rows = conn.execute("SELECT * FROM novel_generation_jobs WHERE project_id=? ORDER BY updated_at DESC", (project_id,)).fetchall()
        return [self._job(row) for row in rows]

    @staticmethod
    def _project(row) -> NovelProject:
        return NovelProject(row["project_id"], row["name"], row["slug"], row["owner_id"], row["root"], json.loads(row["metadata"] or "{}"), row["version"], row["updated_at"])

    @staticmethod
    def _draft(row) -> NovelDraft:
        return NovelDraft(row["project_id"], row["chapter_no"], row["draft_content"], row["version"], row["status"])

    @staticmethod
    def _job(row) -> GenerationJob:
        return GenerationJob(row["job_id"], row["idempotency_key"], row["project_id"], row["chapter_no"], GenerationJobStatus(row["status"]), row["prompt"], row["draft_content"], json.loads(row["review_result"] or "{}"), row["error"], row["attempts"], row["progress"], row["heartbeat_at"], row["version"])


class LegacyNovelRepository:
    """全局 chapter_notes 的兼容适配器。"""
    def get_project(self, project_id: str | None = None) -> NovelProject:
        return NovelProject(project_id or "default", "默认小说", "default")

    def get_chapter(self, chapter_no: str, project_id: str | None = None) -> Chapter | None:
        from app.services import chapter_analysis
        note = chapter_analysis.get_chapter_note(chapter_no)
        if not note:
            return None
        return Chapter(str(note["chapter"]), "", note["summary"], project_id or "default", "archived")

    def list_chapters(self, project_id: str | None = None) -> list[Chapter]:
        from app.models.database import connect
        conn = connect()
        rows = conn.execute("SELECT chapter, summary FROM chapter_notes ORDER BY CAST(chapter AS INTEGER)").fetchall()
        return [Chapter(str(r["chapter"]), "", r["summary"], project_id or "default", "archived") for r in rows]
