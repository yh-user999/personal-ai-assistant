"""小说项目与生成工作流 API。"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth import require_roles
from app.models.database import db_connection
from app.novel.domain import (
    GenerationJobStatus,
    chapter_payload,
    draft_payload,
    job_payload,
    project_payload,
)
from app.novel.file_store import NovelFileStore
from app.novel.index import (
    file_index_status,
    rebuild_chapter_index,
    search_chapters,
    sync_file_index,
)
from app.novel.repository import SQLiteNovelRepository
from app.novel.workflow import NovelWorkflow

router = APIRouter()


class NovelError(BaseModel):
    code: str
    message: str


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=NovelError(code=code, message=message).model_dump())


class ProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    slug: str | None = Field(None, max_length=100)
    root: str | None = Field(None, max_length=500)


class ProjectUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    root: str | None = Field(None, max_length=500)
    metadata: dict | None = None
    expected_version: int | None = Field(None, ge=1)


class MemberRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=200)
    role: str = Field("member", pattern="^(member|editor|owner)$")


class ChapterRequest(BaseModel):
    chapter_no: str = Field(..., min_length=1, max_length=30)
    title: str = Field("", max_length=200)
    content: str = Field("", max_length=2_000_000)
    draft_content: str = Field("", max_length=2_000_000)
    expected_version: int | None = Field(None, ge=1)


class JobRequest(BaseModel):
    chapter_no: str = Field(..., min_length=1, max_length=30)
    prompt: str = Field("", max_length=20_000)
    idempotency_key: str = Field(..., min_length=1, max_length=200)
    draft_content: str = Field("", max_length=2_000_000)


class ReviewRequest(BaseModel):
    ok: bool
    reply: str = Field("", max_length=20_000)
    problems: list[dict] = Field(default_factory=list, max_length=100)


def _repo(request: Request) -> tuple[SQLiteNovelRepository, str]:
    auth = require_roles(request, "owner", "internal")
    user_id = auth.subject or "owner"
    return SQLiteNovelRepository(owner_id=user_id), user_id


def _audit(user_id: str, project_id: str, action: str, target: str, success: bool = True, summary: dict | None = None) -> None:
    import json
    from datetime import datetime, timezone
    with db_connection() as conn:
        conn.execute("INSERT INTO novel_audit_logs(user_id,project_id,action,target,summary,success,created_at) VALUES(?,?,?,?,?,?,?)", (user_id, project_id, action, target, json.dumps(summary or {}, ensure_ascii=False), int(success), datetime.now(timezone.utc).isoformat()))


@router.get("/novel/projects")
def list_projects(request: Request):
    repo, user_id = _repo(request)
    return {"projects": [project_payload(p) for p in repo.list_projects(user_id)]}


@router.post("/novel/projects")
def create_project(req: ProjectRequest, request: Request):
    repo, user_id = _repo(request)
    project = repo.create_project(req.name, slug=req.slug, root=req.root, owner_id=user_id)
    _audit(user_id, project.project_id, "project.create", project.project_id)
    return project_payload(project)


@router.patch("/novel/projects/{project_id}")
def update_project(project_id: str, req: ProjectUpdateRequest, request: Request):
    repo, user_id = _repo(request)
    if not repo.can_access(project_id, user_id, write=True):
        raise _error(403, "project_write_forbidden", "无项目写入权限")
    try:
        project = repo.update_project(project_id, name=req.name, root=req.root, metadata=req.metadata, expected_version=req.expected_version)
    except KeyError as exc:
        raise _error(404, "project_not_found", "项目不存在") from exc
    except ValueError as exc:
        raise _error(409, "project_version_conflict", str(exc)) from exc
    _audit(user_id, project_id, "project.update", project_id)
    return project_payload(project)


@router.put("/novel/projects/{project_id}/members")
def add_member(project_id: str, req: MemberRequest, request: Request):
    repo, user_id = _repo(request)
    if not repo.can_access(project_id, user_id, write=True):
        raise _error(403, "project_admin_forbidden", "无项目管理权限")
    repo.add_member(project_id, req.user_id, req.role)
    _audit(user_id, project_id, "member.upsert", req.user_id, summary={"role": req.role})
    return {"project_id": project_id, "user_id": req.user_id, "role": req.role}


@router.delete("/novel/projects/{project_id}/members/{member_id}")
def remove_member(project_id: str, member_id: str, request: Request):
    repo, user_id = _repo(request)
    if not repo.can_access(project_id, user_id, write=True):
        raise _error(403, "project_admin_forbidden", "无项目管理权限")
    try:
        repo.remove_member(project_id, member_id)
    except KeyError as exc:
        raise _error(404, "project_not_found", "项目不存在") from exc
    except ValueError as exc:
        raise _error(409, "owner_protected", str(exc)) from exc
    _audit(user_id, project_id, "member.remove", member_id)
    return {"project_id": project_id, "user_id": member_id, "removed": True}


@router.get("/novel/projects/{project_id}/members")
def list_members(project_id: str, request: Request):
    repo, user_id = _repo(request)
    if not repo.can_access(project_id, user_id):
        raise _error(404, "project_not_found", "项目不存在")
    with db_connection() as conn:
        rows = conn.execute("SELECT user_id, role, created_at FROM novel_project_members WHERE project_id=? ORDER BY user_id", (project_id,)).fetchall()
    return {"members": [dict(row) for row in rows]}


@router.get("/novel/projects/{project_id}/chapters")
def list_chapters(project_id: str, request: Request):
    repo, user_id = _repo(request)
    if not repo.can_access(project_id, user_id):
        raise _error(404, "project_not_found", "项目不存在")
    return {"chapters": [chapter_payload(c) for c in repo.list_chapters(project_id)]}


@router.get("/novel/projects/{project_id}/chapters/search")
def search_project_chapters(project_id: str, request: Request, q: str = Query(..., min_length=1, max_length=200), limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    repo, user_id = _repo(request)
    if not repo.can_access(project_id, user_id):
        raise _error(404, "project_not_found", "项目不存在")
    # 只读搜索：不再隐式重建索引（重建由 POST /index/rebuild 显式触发）。
    try:
        results = search_chapters(project_id, q, limit=limit, offset=offset)
    except sqlite3.OperationalError:
        # FTS 查询语法错误（如裸的 NEAR/通配符）不视为服务端错误
        results = []
    return {"project_id": project_id, "query": q, "results": results}


@router.get("/novel/projects/{project_id}/chapters/{chapter_no}")
def get_chapter(project_id: str, chapter_no: str, request: Request):
    repo, user_id = _repo(request)
    if not repo.can_access(project_id, user_id):
        raise _error(404, "project_not_found", "项目不存在")
    chapter = repo.get_chapter(chapter_no, project_id)
    if not chapter:
        raise _error(404, "chapter_not_found", "章节不存在")
    return chapter_payload(chapter)


@router.put("/novel/projects/{project_id}/chapters")
def upsert_chapter(project_id: str, req: ChapterRequest, request: Request):
    repo, user_id = _repo(request)
    if not repo.can_access(project_id, user_id, write=True):
        raise _error(403, "project_write_forbidden", "无项目写入权限")
    draft = repo.upsert_chapter(project_id, req.chapter_no, title=req.title, content=req.content, draft_content=req.draft_content, expected_version=req.expected_version)
    _audit(user_id, project_id, "chapter.write", req.chapter_no)
    return draft_payload(draft)


@router.post("/novel/projects/{project_id}/jobs")
def create_job(project_id: str, req: JobRequest, request: Request):
    repo, user_id = _repo(request)
    if not repo.can_access(project_id, user_id, write=True):
        raise _error(403, "project_write_forbidden", "无项目写入权限")
    job = repo.create_job(project_id, req.chapter_no, req.idempotency_key, req.prompt)
    if req.draft_content:
        repo.update_job(job.job_id, GenerationJobStatus.AWAITING_CONFIRMATION, draft_content=req.draft_content)
        job = repo.get_job(job.job_id)
    _audit(user_id, project_id, "job.create", job.job_id)
    return job_payload(job)


@router.get("/novel/projects/{project_id}/jobs")
def list_jobs(project_id: str, request: Request):
    repo, user_id = _repo(request)
    if not repo.can_access(project_id, user_id):
        raise _error(404, "project_not_found", "项目不存在")
    return {"jobs": [job_payload(j) for j in repo.list_jobs(project_id)]}


@router.post("/novel/projects/{project_id}/jobs/{job_id}/review")
def review_job(project_id: str, job_id: str, req: ReviewRequest, request: Request):
    repo, user_id = _repo(request)
    if not repo.can_access(project_id, user_id, write=True):
        raise _error(403, "project_write_forbidden", "无项目写入权限")
    job = repo.get_job(job_id)
    if not job or job.project_id != project_id:
        raise _error(404, "job_not_found", "任务不存在")
    status = GenerationJobStatus.AWAITING_CONFIRMATION if req.ok else GenerationJobStatus.FAILED
    updated = repo.update_job(job_id, status, review_result={"ok": req.ok, "reply": req.reply, "problems": req.problems}, error="" if req.ok else req.reply)
    _audit(user_id, project_id, "job.review", job_id, summary={"ok": req.ok, "problem_count": len(req.problems)})
    return job_payload(updated)


@router.post("/novel/projects/{project_id}/jobs/{job_id}/confirm")
def confirm_publish(project_id: str, job_id: str, request: Request):
    repo, user_id = _repo(request)
    if not repo.can_access(project_id, user_id, write=True):
        raise _error(403, "project_write_forbidden", "无项目写入权限")
    job = repo.get_job(job_id)
    if not job or job.project_id != project_id:
        raise _error(404, "job_not_found", "任务不存在")
    updated = repo.publish_job(job_id, expected_version=job.version)
    project = repo.get_project(project_id)
    chapter = repo.get_chapter(job.chapter_no, project_id)
    file_path = None
    if project.root and chapter:
        try:
            file_path = NovelFileStore(project.root).write_chapter(chapter.chapter_no, chapter.content, title=chapter.title)
        except Exception as exc:
            _audit(user_id, project_id, "chapter.file_sync", job_id, success=False, summary={"error": type(exc).__name__})
            raise _error(500, "file_sync_failed", "章节已入库，但文件同步失败") from exc
    _audit(user_id, project_id, "job.publish", job_id, summary={"file_path": file_path})
    return {**job_payload(updated), "file_path": file_path}


@router.post("/novel/projects/{project_id}/jobs/{job_id}/file-sync")
def retry_file_sync(project_id: str, job_id: str, request: Request):
    repo, user_id = _repo(request)
    if not repo.can_access(project_id, user_id, write=True):
        raise _error(403, "project_write_forbidden", "无项目写入权限")
    job = repo.get_job(job_id)
    if not job or job.project_id != project_id:
        raise _error(404, "job_not_found", "任务不存在")
    if job.status is not GenerationJobStatus.PUBLISHED:
        raise _error(409, "job_not_published", "只有已发布任务可以同步文件")
    project = repo.get_project(project_id)
    chapter = repo.get_chapter(job.chapter_no, project_id)
    if not project.root or not chapter:
        raise _error(409, "file_sync_unavailable", "项目或章节缺少文件同步信息")
    try:
        path = NovelFileStore(project.root).write_chapter(chapter.chapter_no, chapter.content, title=chapter.title)
    except Exception as exc:
        _audit(user_id, project_id, "chapter.file_sync", job_id, success=False, summary={"error": type(exc).__name__})
        raise _error(500, "file_sync_failed", "文件同步失败") from exc
    _audit(user_id, project_id, "chapter.file_sync", job_id, summary={"file_path": path, "retry": True})
    return {"job_id": job_id, "file_path": path, "synced": True}


@router.get("/novel/projects/{project_id}/consistency")
def consistency(project_id: str, request: Request):
    repo, user_id = _repo(request)
    if not repo.can_access(project_id, user_id):
        raise _error(404, "project_not_found", "项目不存在")
    from app.novel.consistency import check_project
    return check_project(project_id, repository=repo)


@router.get("/novel/projects/{project_id}/audit")
def list_audit(project_id: str, request: Request, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), action: str | None = Query(None, max_length=100), success: bool | None = None):
    repo, user_id = _repo(request)
    if not repo.can_access(project_id, user_id):
        raise _error(404, "project_not_found", "项目不存在")
    clauses = ["project_id=?"]
    params: list[object] = [project_id]
    if action:
        clauses.append("action=?")
        params.append(action)
    if success is not None:
        clauses.append("success=?")
        params.append(int(success))
    where = " AND ".join(clauses)
    with db_connection() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM novel_audit_logs WHERE {where}", params).fetchone()[0]
        rows = conn.execute(f"SELECT action, target, summary, success, created_at FROM novel_audit_logs WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
    return {"audit": [dict(row) for row in rows], "limit": limit, "offset": offset, "total": total}


@router.post("/novel/projects/{project_id}/jobs/{job_id}/cancel")
def cancel_job(project_id: str, job_id: str, request: Request):
    repo, user_id = _repo(request)
    if not repo.can_access(project_id, user_id, write=True):
        raise _error(403, "project_write_forbidden", "无项目写入权限")
    job = repo.get_job(job_id)
    if not job or job.project_id != project_id:
        raise _error(404, "job_not_found", "任务不存在")
    workflow = NovelWorkflow.from_job(job, repo)
    try:
        workflow.cancel()
    except ValueError as exc:
        raise _error(409, "invalid_job_transition", str(exc)) from exc
    _audit(user_id, project_id, "job.cancel", job_id)
    return job_payload(repo.get_job(job_id))


@router.post("/novel/projects/{project_id}/jobs/{job_id}/retry")
def retry_job(project_id: str, job_id: str, request: Request, max_attempts: int = Query(3, ge=1, le=20)):
    repo, user_id = _repo(request)
    if not repo.can_access(project_id, user_id, write=True):
        raise _error(403, "project_write_forbidden", "无项目写入权限")
    job = repo.get_job(job_id)
    if not job or job.project_id != project_id:
        raise _error(404, "job_not_found", "任务不存在")
    try:
        updated = repo.retry_job(job_id, max_attempts=max_attempts)
    except ValueError as exc:
        raise _error(409, "invalid_job_transition", str(exc)) from exc
    _audit(user_id, project_id, "job.retry", job_id, summary={"max_attempts": max_attempts})
    return job_payload(updated)


@router.post("/novel/projects/{project_id}/jobs/{job_id}/heartbeat")
def heartbeat_job(project_id: str, job_id: str, request: Request, progress: int = Query(..., ge=0, le=100), expected_version: int | None = Query(None, ge=1)):
    repo, user_id = _repo(request)
    if not repo.can_access(project_id, user_id, write=True):
        raise _error(403, "project_write_forbidden", "无项目写入权限")
    job = repo.get_job(job_id)
    if not job or job.project_id != project_id:
        raise _error(404, "job_not_found", "任务不存在")
    try:
        updated = repo.heartbeat_job(job_id, progress=progress, expected_version=expected_version)
    except ValueError as exc:
        raise _error(409, "invalid_job_transition", str(exc)) from exc
    return job_payload(updated)


@router.post("/novel/projects/{project_id}/index/rebuild")
def rebuild_project_index(project_id: str, request: Request):
    repo, user_id = _repo(request)
    if not repo.can_access(project_id, user_id, write=True):
        raise _error(403, "project_write_forbidden", "无项目写入权限")
    count = rebuild_chapter_index(project_id)
    project = repo.get_project(project_id)
    file_result = sync_file_index(project_id, project.root) if project.root else {"files": 0, "consistent": True}
    _audit(user_id, project_id, "index.rebuild", project_id, summary={"chapters": count, "files": file_result.get("files", 0)})
    return {"project_id": project_id, "chapters": count, "files": file_result}


@router.get("/novel/projects/{project_id}/index/status")
def project_index_status(project_id: str, request: Request):
    repo, user_id = _repo(request)
    if not repo.can_access(project_id, user_id):
        raise _error(404, "project_not_found", "项目不存在")
    return {"chapters": len(repo.list_chapters(project_id)), "files": file_index_status(project_id)}
