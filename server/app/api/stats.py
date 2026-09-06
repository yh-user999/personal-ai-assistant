"""行为统计接口：供 Web 仪表盘 / 桌面端查询。"""
import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request

from app.auth import require_roles
from app.core.memory import owner_user_id
from app.models import repo

router = APIRouter()


def _owner_scope(request: Request) -> str:
    """统计端点只允许主人/内部调用，并始终使用认证主体。"""
    auth = require_roles(request, "owner", "internal")
    return str(auth.subject or owner_user_id())


@router.get("/stats/summary")
async def stats_summary(request: Request, days: int = 7) -> dict:
    """总览：对话数、提交数、应用时长 Top、浏览域名 Top、日志数。"""
    owner = _owner_scope(request)
    days = max(1, min(int(days), 90))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    blocks = await asyncio.to_thread(repo.stats_summary_blocks, owner, since)
    return {
        "days": days,
        "messages": blocks["n_msg"],
        "git_commits": blocks["n_commit"],
        "work_logs": blocks["n_log"],
        "top_apps": [
            {"name": a["name"], "hours": round((a["secs"] or 0) / 3600, 1)}
            for a in blocks["apps"]
        ],
        "top_domains": [{"name": b["name"], "count": b["cnt"]} for b in blocks["browsers"]],
    }


@router.get("/stats/hourly")
async def stats_hourly(request: Request, days: int = 7) -> dict:
    """按小时分布：对话活跃时段 + 提交时段。"""
    owner = _owner_scope(request)
    days = max(1, min(int(days), 90))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = await asyncio.to_thread(repo.hourly_memory_counts, owner, since)
    hours = [0] * 24
    for row in rows:
        hours[row["h"]] = row["c"]
    return {"hours": hours}


@router.get("/stats/cost")
async def stats_cost(request: Request, days: int = 7) -> dict:
    """成本画像（P4）：LLM token 累计 + 检索决策轨迹聚合。"""
    import asyncio

    from app.services.cost_report import cost_report

    owner = _owner_scope(request)
    return await asyncio.to_thread(cost_report, days, owner)
