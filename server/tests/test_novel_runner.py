"""小说生成执行器测试。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.models import database
from app.novel.domain import GenerationJobStatus
from app.novel.repository import SQLiteNovelRepository
from app.novel.runner import retry_failed_jobs, run_one_job


def test_runner_persists_draft_and_is_idempotent(db):
    repo = SQLiteNovelRepository(owner_id="owner")
    repo.create_project("书", project_id="p", slug="runner-book")
    repo.create_job("p", "1", "runner-1", "片段")

    async def generator(prompt):
        assert prompt == "片段"
        return "生成正文"

    result = asyncio.run(run_one_job(repository=repo, generator=generator))
    assert result.status is GenerationJobStatus.AWAITING_CONFIRMATION
    assert repo.get_draft("p", "1").content == "生成正文"
    assert repo.claim_next_job() is None


def test_runner_marks_failure_and_requeues_with_budget(db):
    repo = SQLiteNovelRepository(owner_id="owner")
    repo.create_project("书", project_id="p", slug="failure-book")
    job = repo.create_job("p", "1", "failure-1", "失败")

    async def failing(_prompt):
        raise RuntimeError("模型不可用")

    failed = asyncio.run(run_one_job(repository=repo, generator=failing))
    assert failed.status is GenerationJobStatus.FAILED
    assert failed.attempts == 1
    assert asyncio.run(retry_failed_jobs(repository=repo)) == 1
    assert repo.get_job(job.job_id).status is GenerationJobStatus.QUEUED


def test_stale_generating_job_is_requeued(db):
    repo = SQLiteNovelRepository(owner_id="owner")
    repo.create_project("书", project_id="p", slug="stale-book")
    job = repo.create_job("p", "1", "stale-1")
    claimed = repo.claim_next_job()
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with database.db_connection() as conn:
        conn.execute("UPDATE novel_generation_jobs SET updated_at=? WHERE job_id=?", (old, job.job_id))
    assert repo.recover_stale_jobs(timeout_seconds=60) == 1
    assert repo.get_job(claimed.job_id).status is GenerationJobStatus.QUEUED


def test_retry_budget_keeps_failed_job(db):
    repo = SQLiteNovelRepository(owner_id="owner")
    repo.create_project("书", project_id="p", slug="budget-book")
    job = repo.create_job("p", "1", "budget-1")
    for _ in range(3):
        claimed = repo.claim_next_job()
        repo.update_job(claimed.job_id, GenerationJobStatus.FAILED, error="x", expected_version=claimed.version)
        if claimed.attempts < 3:
            repo.retry_job(claimed.job_id, max_attempts=3)
    assert repo.get_job(job.job_id).attempts == 3
    assert repo.retry_job(job.job_id, max_attempts=3).status is GenerationJobStatus.FAILED
