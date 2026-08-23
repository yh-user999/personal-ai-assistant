"""APScheduler 定时任务管理：
- consolidation: 每 4h 摘要整合（碎片消息 → summary/facts）
- weekly_report: 每周日晚生成《学习进度反思》
- eviction: 每 6h 淘汰 noise（7 天）/ 过期 chat（30 天）
"""
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings


class SchedulerManager:
    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

    async def start(self) -> None:
        from app.services.consolidation import consolidate_recent
        from app.services.weekly_reflect import run_weekly_reflect
        from app.services.analyzer import evict_stale

        # M1：摘要整合（先手动触发验证，再放开定时）
        # self.scheduler.add_job(
        #     lambda: asyncio.create_task(consolidate_recent()),
        #     "interval", hours=settings.consolidation_interval_hours,
        #     id="consolidation",
        # )
        # M3：每周反思
        # self.scheduler.add_job(
        #     lambda: asyncio.create_task(run_weekly_reflect()),
        #     "cron", day_of_week=settings.weekly_report_weekday,
        #     hour=settings.weekly_report_hour, id="weekly_reflect",
        # )
        # 淘汰（可先手动）
        # self.scheduler.add_job(
        #     lambda: asyncio.create_task(evict_stale()),
        #     "interval", hours=6, id="eviction",
        # )
        self.scheduler.start()

    async def stop(self) -> None:
        self.scheduler.shutdown(wait=False)
