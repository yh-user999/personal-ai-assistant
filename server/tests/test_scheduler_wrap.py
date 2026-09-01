"""调度器 wrapper 回归：同步任务不再被 await 报 TypeError。

背景：daily_backup 是同步函数（返回 dict），旧 _wrap_job 一律
`await func(...)`，报 "object dict can't be used in 'await' expression"。
修复后同步任务走 asyncio.to_thread。
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")
os.environ.setdefault("DEPLOYMENT_ENV", "test")

from app.core.scheduler import _wrap_job  # noqa: E402


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


def test_wrap_job_failure_alerts(monkeypatch):
    alerts = []

    async def fake_alert(text):
        alerts.append(text)

    monkeypatch.setattr("app.core.scheduler._push_alert", fake_alert)
    wrapped = _wrap_job(_boom, "t_boom")
    result = asyncio.run(wrapped())
    assert result is None
    assert any("t_boom 执行失败" in a and "boom" in a for a in alerts)
