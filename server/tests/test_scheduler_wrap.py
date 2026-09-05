"""调度器 wrapper 回归：同步任务不再被 await 报 TypeError。

背景：daily_backup 是同步函数（返回 dict），旧 _wrap_job 一律
`await func(...)`，报 "object dict can't be used in 'await' expression"。
修复后同步任务走 asyncio.to_thread。
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")
os.environ.setdefault("DEPLOYMENT_ENV", "test")

from app.core.scheduler import _wrap_job


def _sync_job(a, b=1):
    return {"sum": a + b}


async def _async_job(x):
    return x * 2


def _boom():
    raise RuntimeError("boom")


def test_wrap_sync_job_returns_dict():
    """同步任务（如 run_daily_backup）包装后可执行，不再 await dict。"""
    wrapped = _wrap_job(_sync_job, "t_sync")
    result = asyncio.run(wrapped(2, b=3))
    assert result == {"sum": 5}


def test_wrap_async_job():
    wrapped = _wrap_job(_async_job, "t_async")
    result = asyncio.run(wrapped(4))
    assert result == 8


def test_scheduler_stop_waits_for_running_jobs(monkeypatch):
    from app.core.scheduler import SchedulerManager

    manager = SchedulerManager()
    calls = []

    class FakeScheduler:
        running = True

        def shutdown(self, **kwargs):
            calls.append(kwargs)

    manager.scheduler = FakeScheduler()
    manager._owns_scheduler = True

    async def fake_close():
        calls.append("close")

    monkeypatch.setattr("app.services.qq_push.aclose", fake_close)
    asyncio.run(manager.stop())
    assert calls[0] == {"wait": True}
    assert calls[-1] == "close"
    asyncio.run(manager.stop())
    assert calls.count("close") == 1


def test_scheduler_add_forwards_concurrency_limit(monkeypatch):
    from app.core.scheduler import SchedulerManager

    manager = SchedulerManager()
    calls = []

    class FakeScheduler:
        def add_job(self, func, trigger, **kwargs):
            calls.append((func, trigger, kwargs))

    manager.scheduler = FakeScheduler()
    manager._add("novel_generation", _async_job, "interval", seconds=60, max_instances=1)
    assert calls[0][1] == "interval"
    assert calls[0][2]["max_instances"] == 1
    assert calls[0][2]["misfire_grace_time"] == 3600
    assert calls[0][2]["coalesce"] is True


def test_scheduler_rejects_duplicate_active_manager(monkeypatch):
    from app.core.scheduler import SchedulerManager

    existing = object()
    monkeypatch.setattr(SchedulerManager, "_active_manager", existing)
    manager = SchedulerManager()
    manager.scheduler = type("FakeScheduler", (), {"get_jobs": lambda self: []})()

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("duplicate scheduler must not initialize jobs")

    monkeypatch.setattr(manager, "_add", fail_if_called)
    asyncio.run(manager.start())
    assert manager._started is True
    assert manager._stopped is True
    monkeypatch.setattr(SchedulerManager, "_active_manager", None)


def test_stale_generation_jobs_are_recovered_before_scheduler_run(db):
    from datetime import datetime, timedelta, timezone

    from app.models import database
    from app.novel.domain import GenerationJobStatus
    from app.novel.repository import SQLiteNovelRepository
    from app.novel.runner import recover_and_run_pending

    repo = SQLiteNovelRepository(owner_id="owner")
    repo.create_project("书", project_id="scheduler", slug="scheduler-book")
    job = repo.create_job("scheduler", "1", "scheduler-stale")
    claimed = repo.claim_next_job()
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with database.db_connection() as conn:
        conn.execute("UPDATE novel_generation_jobs SET updated_at=? WHERE job_id=?", (old, job.job_id))

    async def generator(_prompt):
        return "调度生成结果"

    result = asyncio.run(recover_and_run_pending(repository=repo, generator=generator))
    assert result and result[0].status is GenerationJobStatus.AWAITING_CONFIRMATION
    assert repo.get_job(claimed.job_id).attempts == 2


def test_wrap_job_failure_alerts(monkeypatch):
    alerts = []

    async def fake_alert(text):
        alerts.append(text)

    monkeypatch.setattr("app.core.scheduler._push_alert", fake_alert)
    wrapped = _wrap_job(_boom, "t_boom")
    result = asyncio.run(wrapped())
    assert result is None
    assert any("t_boom 执行失败" in a and "boom" in a for a in alerts)
