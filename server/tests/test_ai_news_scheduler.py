"""每日 AI 资讯日报定时任务注册回归。"""
from __future__ import annotations

import asyncio

from app.config import settings
from app.core.scheduler import SchedulerManager


def test_ai_news_job_registers_at_configured_time(monkeypatch):
    calls = []

    class FakeScheduler:
        running = False

        def add_job(self, func, trigger, **kwargs):
            calls.append((getattr(func, "__name__", ""), trigger, kwargs))

        def start(self):
            self.running = True

        def shutdown(self, **kwargs):
            self.running = False

        def get_jobs(self):
            return []

    async def fake_ai_news_job(**kwargs):
        return kwargs

    manager = SchedulerManager(ai_news_job=fake_ai_news_job)
    manager.scheduler = FakeScheduler()
    monkeypatch.setattr(settings, "ai_news_digest_enabled", True)
    monkeypatch.setattr(settings, "ai_news_digest_hour", 8)
    monkeypatch.setattr(settings, "ai_news_digest_minute", 0)
    monkeypatch.setattr(manager, "_add", lambda job_id, func, trigger, **kwargs: calls.append((job_id, trigger, kwargs)))
    monkeypatch.setattr(SchedulerManager, "_active_manager", None)

    try:
        asyncio.run(manager.start())
    finally:
        asyncio.run(manager.stop())

    matching = [item for item in calls if item[0] == "ai_news_digest"]
    assert len(matching) == 1
    assert matching[0][1] == "cron"
    assert matching[0][2]["hour"] == 8
    assert matching[0][2]["minute"] == 0
    assert matching[0][2]["kwargs"] == {"user_id": "owner"}
