"""APScheduler 定时任务管理：
- consolidation: 每 4h 摘要整合（碎片消息 → summary/facts），窗口与间隔对齐
- weekly_report: 每周日晚 21:00 生成《学习进度反思》
- eviction: 每 6h 淘汰 noise / 低 importance chat

时区：Asia/Shanghai（周报"周日 21:00"按北京时间触发）。
协程函数直接交给 add_job（AsyncIOScheduler 会在事件循环上调度），
不用 lambda+create_task——后者不保留任务引用，可能被 GC 中途回收、任务无声消失。

可靠性（v0.3.2）：
- 全部 cron 任务带 misfire_grace_time=3600 + coalesce=True——服务器在
  触发窗口宕机，恢复后 1 小时内补跑一次而不是永久错过（旧实现错过即丢，
  daily_summaries 的 UNIQUE 约束还会堵住手动补跑）。
- 所有任务包 _safe_job wrapper：异常捕获后经 qq_push 通道给主人推告警——
  定时任务失败不再只落在日志里（QQ 是唯一必达通道）。
"""
import asyncio
import inspect
import logging

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings

logger = logging.getLogger("assistant.scheduler")

GRACE_SECONDS = 3600  # 错过触发后 1 小时内仍补跑


async def _push_alert(text: str) -> None:
    """任务失败告警 → 主人 QQ（唯一必达通道）。推送失败仅记日志。

    走 qq_push.send_private 单一出口——原先本函数自建 httpx.AsyncClient
    并重复实现推送逻辑（三处之一，见 qq_push.send_private 的注释）。
    """
    try:
        from app.config import settings as _s

        if not (_s.qq_push_url and _s.qq_admin_id):
            logger.warning("定时任务失败且 QQ 通道未配置: %s", text)
            return
        from app.services.qq_push import send_private

        if not await send_private(text):
            logger.error("任务失败告警推送未送达: %s", text)
    except (httpx.HTTPError, RuntimeError, OSError, ValueError, TypeError) as e:
        logger.error("任务失败告警推送也失败: %s（原始告警: %s）", e, text)


def _wrap_job(func, job_id: str):
    """定时任务 wrapper 工厂：异常捕获 → QQ 告警，不再静默。

    兼容同步与异步任务：同步任务（如 run_daily_backup）经 asyncio.to_thread
    在后台线程执行——否则 await 普通函数返回的 dict 会报
    "object dict can't be used in 'await' expression"，且阻塞 I/O
    （sqlite3 热备份）也不该占住事件循环。

    用闭包而非 functools.partial——APScheduler 会用 signature() 校验
    可调用对象，partial 固定位置参数后与 job args 冲突报 ValueError。
    """

    async def _run(*args, **kwargs):
        try:
            if inspect.iscoroutinefunction(func):
                return await func(*args, **kwargs)
            return await asyncio.to_thread(func, *args, **kwargs)
        except Exception as e:
            logger.exception("定时任务 %s 失败", job_id)
            await _push_alert(f"{job_id} 执行失败：{type(e).__name__}: {e}")

    _run.__name__ = func.__name__  # APScheduler 日志可读性
    return _run


class SchedulerManager:
    _active_manager = None

    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        self._started = False
        self._stopped = False
        self._owns_scheduler = False

    def _add(self, job_id: str, func, trigger: str, **kw) -> None:
        """统一 add_job：misfire 补跑 + coalesce + 安全 wrapper。"""
        self.scheduler.add_job(
            _wrap_job(func, job_id),
            trigger,
            id=job_id,
            misfire_grace_time=GRACE_SECONDS,
            coalesce=True,
            **kw,
        )

    async def start(self) -> None:
        """幂等启动：避免重复注册任务（多次 lifespan/测试启动）。"""
        if self._started and not self._stopped:
            logger.info("定时任务已启动，跳过重复 start")
            return
        if self._stopped:
            logger.warning("定时任务已停止，拒绝重启同一实例")
            return
        if SchedulerManager._active_manager is not None:
            logger.warning("已有调度器实例运行，跳过重复启动")
            self._started = True
            self._stopped = True
            return
        SchedulerManager._active_manager = self
        self._owns_scheduler = True
        self._started = True
        from app.novel.runner import recover_and_run_pending
        from app.services.analyzer import evict_stale
        from app.services.backup import run_daily_backup
        from app.services.consolidation import consolidate_recent
        from app.services.daily_summary import run_daily_summary
        from app.services.profile import refresh_profile
        from app.services.progress_sync import sync_progress_to_bot
        from app.services.qq_push import push_reminders
        from app.services.weekly_reflect import run_weekly_reflect

        # 摘要整合：每 4h 一次，整合窗口 = 间隔（4h），不漏消息
        self._add(
            "consolidation", consolidate_recent, "interval",
            hours=settings.consolidation_interval_hours,
            args=[settings.consolidation_interval_hours],
        )
        # 画像刷新：周报前一小时（周日 20:00），周报生成时能读到最新画像
        self._add(
            "profile_refresh", refresh_profile, "cron",
            day_of_week=settings.weekly_report_weekday,
            hour=max(0, settings.weekly_report_hour - 1),
        )
        # 每日画像（6.23 课）：凌晨 4:30 滚动更新，画像不再是"一周前的你"
        self._add("profile_daily", refresh_profile, "cron", hour=4, minute=30)
        # 每周反思：周日 21:00（Asia/Shanghai）
        self._add(
            "weekly_reflect", run_weekly_reflect, "cron",
            day_of_week=settings.weekly_report_weekday,
            hour=settings.weekly_report_hour,
        )
        # 每日小结：每晚 22:00（错过窗口 → 1h 内补跑，不再永久缺失）
        self._add("daily_summary", run_daily_summary, "cron", hour=22)
        # 淘汰：noise 30 天 / 低 importance chat 365 天（同步清 FTS 索引）
        self._add("eviction", evict_stale, "interval", hours=6)
        # 每日备份：凌晨 3:00（避开周报/整合高峰；热备份+滚动保留 7 份）
        self._add("daily_backup", run_daily_backup, "cron", hour=3)
        # 每日进度同步：4:10 把仓库 docs/*.md 重灌知识库 + 刷新课程进度事实
        self._add("progress_sync", sync_progress_to_bot, "cron", hour=4, minute=10)

        # 主动开口（默认关闭）：小结生成后挑一句主动推 QQ。
        # 未启用时任务本身仍注册，run_initiative 里 should_speak 直接返回
        # skipped——开关只在 .env 改一行，不用动代码/重排任务。
        from app.services.initiative import run_initiative

        self._add(
            "initiative", run_initiative, "cron",
            hour=settings.initiative_hour, minute=settings.initiative_minute,
        )
        # 小说生成任务：每分钟最多执行一个，queued 任务在重启后自动续跑。
        self._add("novel_generation", recover_and_run_pending, "interval", seconds=60, max_instances=1)

        # QQ 提醒推送（第 8 课）：每分钟检查到期提醒并推主人 QQ（唯一通道）。
        # 高频通道不套告警 wrapper（失败每分钟告警反而轰炸），自身已留痕。
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
        """关闭并等待正在执行的调度任务完成，且可安全重复调用。"""
        if self._stopped:
            return
        self._stopped = True
        from app.services.qq_push import aclose

        if not self._owns_scheduler:
            return
        # 先停止接收新任务，再等待运行中的任务收尾，避免关闭时丢推送。
        if self.scheduler.running:
            self.scheduler.shutdown(wait=True)
        SchedulerManager._active_manager = None
        await aclose()  # 长驻推送客户端收尾
