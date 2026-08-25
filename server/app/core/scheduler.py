"""APScheduler 定时任务管理：
- consolidation: 每 4h 摘要整合（碎片消息 → summary/facts），窗口与间隔对齐
- weekly_report: 每周日晚 21:00 生成《学习进度反思》
- eviction: 每 6h 淘汰 noise（7 天）/ 过期 chat（30 天）

时区：Asia/Shanghai（周报"周日 21:00"按北京时间触发）。
"""
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings

logger = logging.getLogger("assistant.scheduler")


class SchedulerManager:
    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

    async def start(self) -> None:
        from app.services.consolidation import consolidate_recent
        from app.services.weekly_reflect import run_weekly_reflect
        from app.services.profile import refresh_profile
        from app.services.analyzer import evict_stale

        # 摘要整合：每 4h 一次，整合窗口 = 间隔（4h），不漏消息
        self.scheduler.add_job(
            lambda: asyncio.create_task(
                consolidate_recent(hours=settings.consolidation_interval_hours)
            ),
            "interval",
            hours=settings.consolidation_interval_hours,
            id="consolidation",
        )
        # 画像刷新：周报前一小时（周日 20:00），周报生成时能读到最新画像
        self.scheduler.add_job(
            lambda: asyncio.create_task(refresh_profile()),
            "cron",
            day_of_week=settings.weekly_report_weekday,
            hour=max(0, settings.weekly_report_hour - 1),
            id="profile_refresh",
        )
        # 每周反思：周日 21:00（Asia/Shanghai）
        self.scheduler.add_job(
            lambda: asyncio.create_task(run_weekly_reflect()),
            "cron",
            day_of_week=settings.weekly_report_weekday,
            hour=settings.weekly_report_hour,
            id="weekly_reflect",
        )
        # 淘汰：noise 7 天删除 / chat 30 天
        self.scheduler.add_job(
            lambda: asyncio.create_task(evict_stale()),
            "interval",
            hours=6,
            id="eviction",
        )

        self.scheduler.start()

        # 启动后打印任务清单，便于确认注册成功
        for job in self.scheduler.get_jobs():
            logger.info("定时任务已注册: %s | 下次执行: %s", job.id, job.next_run_time)

    async def stop(self) -> None:
        self.scheduler.shutdown(wait=False)
