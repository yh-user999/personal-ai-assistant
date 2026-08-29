"""APScheduler 定时任务管理：
- consolidation: 每 4h 摘要整合（碎片消息 → summary/facts），窗口与间隔对齐
- weekly_report: 每周日晚 21:00 生成《学习进度反思》
- eviction: 每 6h 淘汰 noise（7 天）/ 过期 chat（30 天）

时区：Asia/Shanghai（周报"周日 21:00"按北京时间触发）。
协程函数直接交给 add_job（AsyncIOScheduler 会在事件循环上调度），
不用 lambda+create_task——后者不保留任务引用，可能被 GC 中途回收、任务无声消失。
"""
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
        from app.services.daily_summary import run_daily_summary
        from app.services.profile import refresh_profile
        from app.services.analyzer import evict_stale
        from app.services.backup import run_daily_backup
        from app.services.progress_sync import sync_progress_to_bot
        from app.services.qq_push import push_reminders

        # 摘要整合：每 4h 一次，整合窗口 = 间隔（4h），不漏消息
        self.scheduler.add_job(
            consolidate_recent,
            "interval",
            hours=settings.consolidation_interval_hours,
            args=[settings.consolidation_interval_hours],
            id="consolidation",
        )
        # 画像刷新：周报前一小时（周日 20:00），周报生成时能读到最新画像
        self.scheduler.add_job(
            refresh_profile,
            "cron",
            day_of_week=settings.weekly_report_weekday,
            hour=max(0, settings.weekly_report_hour - 1),
            id="profile_refresh",
        )
        # 每日画像（6.23 课）：凌晨 4:30 滚动更新，画像不再是"一周前的你"
        self.scheduler.add_job(
            refresh_profile,
            "cron",
            hour=4,
            minute=30,
            id="profile_daily",
        )
        # 每周反思：周日 21:00（Asia/Shanghai）
        self.scheduler.add_job(
            run_weekly_reflect,
            "cron",
            day_of_week=settings.weekly_report_weekday,
            hour=settings.weekly_report_hour,
            id="weekly_reflect",
        )
        # 每日小结：每晚 22:00
        self.scheduler.add_job(
            run_daily_summary,
            "cron",
            hour=22,
            id="daily_summary",
        )
        # 淘汰：noise 7 天删除 / chat 30 天
        self.scheduler.add_job(
            evict_stale,
            "interval",
            hours=6,
            id="eviction",
        )
        # 每日备份：凌晨 3:00（避开周报/整合高峰；热备份+滚动保留 7 份）
        self.scheduler.add_job(
            run_daily_backup,
            "cron",
            hour=3,
            id="daily_backup",
        )
        # 每日进度同步：4:10 把仓库 docs/*.md 重灌知识库 + 刷新课程进度事实
        # （修复"文档更新了 机器人不知道"的流程缺环）
        self.scheduler.add_job(
            sync_progress_to_bot,
            "cron",
            hour=4,
            minute=10,
            id="progress_sync",
        )

        # QQ 提醒推送（第 8 课）：每分钟检查到期提醒并推主人 QQ（唯一通道）
        self.scheduler.add_job(
            push_reminders,
            "interval",
            seconds=60,
            id="qq_reminder_push",
        )

        self.scheduler.start()

        # 启动后打印任务清单，便于确认注册成功
        for job in self.scheduler.get_jobs():
            logger.info("定时任务已注册: %s | 下次执行: %s", job.id, job.next_run_time)

    async def stop(self) -> None:
        self.scheduler.shutdown(wait=False)
