"""小说项目 MCP tools。"""
from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.context import Context

from app.novel.domain import chapter_payload, job_payload, project_payload
from app.novel.index import file_index_status, search_chapters, sync_file_index
from app.novel.repository import SQLiteNovelRepository

from ..audit import audited_tool
from ..permissions import require_confirmed_action, require_read
from ..schemas import bounded_limit, bounded_text, cap_payload


def _repo(ctx):
    return SQLiteNovelRepository(owner_id=ctx.uid)


@audited_tool
async def list_novel_projects(ctx: Context | None = None) -> dict[str, Any]:
    identity = require_read(ctx)
    return cap_payload({"projects": [project_payload(p) for p in _repo(identity).list_projects(identity.uid)]})


@audited_tool
async def list_novel_chapters(project_id: str, limit: int = 100, ctx: Context | None = None) -> dict[str, Any]:
    identity = require_read(ctx)
    project_id = bounded_text(project_id, name="project_id", max_chars=100)
    repo = _repo(identity)
    if not repo.can_access(project_id, identity.uid):
        raise PermissionError("无权访问该小说项目")
    chapters = repo.list_chapters(project_id)[:bounded_limit(limit, name="limit", default=100, maximum=200)]
    return cap_payload({"project_id": project_id, "chapters": [chapter_payload(c) for c in chapters]})


@audited_tool
async def get_novel_job(job_id: str, ctx: Context | None = None) -> dict[str, Any]:
    identity = require_read(ctx)
    job_id = bounded_text(job_id, name="job_id", max_chars=100)
    job = _repo(identity).get_job(job_id)
    if not job or not _repo(identity).can_access(job.project_id, identity.uid):
        raise PermissionError("无权访问该小说任务")
    return cap_payload({"job": job_payload(job)})


@audited_tool
async def publish_novel_job(project_id: str, job_id: str, confirmed: bool = False, ctx: Context | None = None) -> dict[str, Any]:
    identity = require_confirmed_action(ctx, confirmed=confirmed)
    repo = _repo(identity)
    if not repo.can_access(project_id, identity.uid, write=True):
        raise PermissionError("无权发布该小说项目")
    from app.novel.workflow import NovelWorkflow
    job = repo.get_job(job_id)
    if not job or job.project_id != project_id:
        raise ValueError("任务不存在")
    NovelWorkflow.from_job(job, repo).publish()
    repo.upsert_chapter(project_id, job.chapter_no, content=job.draft_content, draft_content=job.draft_content, status="published")
    return cap_payload({"job": job_payload(repo.get_job(job_id))})


@audited_tool
async def retry_novel_job(project_id: str, job_id: str, max_attempts: int = 3, ctx: Context | None = None) -> dict[str, Any]:
    identity = require_confirmed_action(ctx, confirmed=True)
    repo = _repo(identity)
    if not repo.can_access(project_id, identity.uid, write=True):
        raise PermissionError("无权重试该小说任务")
    job = repo.get_job(job_id)
    if not job or job.project_id != project_id:
        raise ValueError("任务不存在")
    return cap_payload({"job": job_payload(repo.retry_job(job_id, max_attempts=max(1, min(max_attempts, 20))))})


@audited_tool
async def cancel_novel_job(project_id: str, job_id: str, confirmed: bool = False, ctx: Context | None = None) -> dict[str, Any]:
    identity = require_confirmed_action(ctx, confirmed=confirmed)
    repo = _repo(identity)
    if not repo.can_access(project_id, identity.uid, write=True):
        raise PermissionError("无权取消该小说任务")
    job = repo.get_job(job_id)
    if not job or job.project_id != project_id:
        raise ValueError("任务不存在")
    from app.novel.workflow import NovelWorkflow
    NovelWorkflow.from_job(job, repo).cancel()
    return cap_payload({"job": job_payload(repo.get_job(job_id))})


@audited_tool
async def search_novel_chapters(project_id: str, query: str, limit: int = 50, offset: int = 0, ctx: Context | None = None) -> dict[str, Any]:
    identity = require_read(ctx)
    repo = _repo(identity)
    if not repo.can_access(project_id, identity.uid):
        raise PermissionError("无权访问该小说项目")
    return cap_payload({"project_id": project_id, "results": search_chapters(project_id, bounded_text(query, name="query", max_chars=200), limit=bounded_limit(limit, name="limit", default=50, maximum=200), offset=max(0, offset))})


@audited_tool
async def sync_novel_file_index(project_id: str, rebuild: bool = False, confirmed: bool = False, ctx: Context | None = None) -> dict[str, Any]:
    identity = require_confirmed_action(ctx, confirmed=confirmed)
    repo = _repo(identity)
    if not repo.can_access(project_id, identity.uid, write=True):
        raise PermissionError("无权同步该小说项目")
    project = repo.get_project(project_id)
    if not project.root:
        raise ValueError("项目未配置文件根目录")
    return cap_payload(sync_file_index(project_id, project.root, rebuild=rebuild))


@audited_tool
async def get_novel_index_status(project_id: str, ctx: Context | None = None) -> dict[str, Any]:
    identity = require_read(ctx)
    repo = _repo(identity)
    if not repo.can_access(project_id, identity.uid):
        raise PermissionError("无权访问该小说项目")
    return cap_payload(file_index_status(project_id))
