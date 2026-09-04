"""小说生成任务执行器：短任务、可重启恢复、失败可重试。"""
from __future__ import annotations

import logging

from app.novel.domain import GenerationJobStatus
from app.novel.repository import SQLiteNovelRepository

logger = logging.getLogger("assistant.novel.runner")


async def run_one_job(*, repository: SQLiteNovelRepository | None = None, generator=None):
    repo = repository or SQLiteNovelRepository()
    job = repo.claim_next_job()
    if not job:
        return None
    try:
        if generator is None:
            from app.services.novel_writing import continue_story
            generator = continue_story
        result = await generator(job.prompt)
        updated = repo.update_job(
            job.job_id,
            GenerationJobStatus.AWAITING_CONFIRMATION,
            draft_content=result,
            review_result={"ok": True, "source": "generation"},
            progress=100,
            expected_version=job.version,
        )
        repo.upsert_chapter(
            job.project_id,
            job.chapter_no,
            draft_content=result,
            status="draft",
        )
        return updated
    except Exception as exc:
        logger.exception("小说生成任务失败: %s", job.job_id)
        try:
            return repo.update_job(
                job.job_id,
                GenerationJobStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
                expected_version=job.version,
            )
        except Exception:
            logger.exception("记录小说生成任务失败状态失败: %s", job.job_id)
            return None


async def retry_failed_jobs(*, repository: SQLiteNovelRepository | None = None, max_attempts: int = 3) -> int:
    repo = repository or SQLiteNovelRepository()
    with __import__("app.models.database", fromlist=["db_connection"]).db_connection() as conn:
        rows = conn.execute("SELECT job_id FROM novel_generation_jobs WHERE status=? AND attempts < ?", (GenerationJobStatus.FAILED.value, max_attempts)).fetchall()
    count = 0
    for row in rows:
        repo.retry_job(row["job_id"], max_attempts=max_attempts)
        count += 1
    return count


async def recover_and_run_pending(*, max_jobs: int = 1, repository=None, generator=None) -> list:
    repo = repository or SQLiteNovelRepository()
    repo.recover_stale_jobs()
    return await run_pending_jobs(max_jobs=max_jobs, repository=repo, generator=generator)


async def run_pending_jobs(*, max_jobs: int = 1, repository=None, generator=None) -> list:
    results = []
    for _ in range(max(1, min(max_jobs, 10))):
        result = await run_one_job(repository=repository, generator=generator)
        if result is None:
            break
        results.append(result)
    return results
