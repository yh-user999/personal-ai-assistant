"""行为统计接口：供 Web 仪表盘 / 桌面端查询。"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request

from app.auth import require_roles
from app.core.memory import _user_scope, owner_user_id
from app.models.database import connect

router = APIRouter()


def _owner_scope(request: Request) -> str:
    """统计端点只允许主人/内部调用，并始终使用认证主体。"""
    auth = require_roles(request, "owner", "internal")
    return str(auth.subject or owner_user_id())


@router.get("/stats/summary")
async def stats_summary(request: Request, days: int = 7) -> dict:
    """总览：对话数、提交数、应用时长 Top、浏览域名 Top、日志数。"""
    owner = _owner_scope(request)
    clause, args = _user_scope(owner)
    days = max(1, min(int(days), 90))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = connect()
    try:
        # behavior_events/work_log 是主人专属采集和台账；只有 memories 需要
        # 按用户列过滤，避免访客聊天进入主人统计。
        apps = conn.execute(
            f"""SELECT name, SUM(CAST(julianday(end_ts)-julianday(start_ts) AS REAL)*86400) AS secs
               FROM behavior_events WHERE kind='app_usage' AND start_ts >= ? AND {clause}
               GROUP BY name ORDER BY secs DESC LIMIT 10""",
            (since, *args),
        ).fetchall()
        browsers = conn.execute(
            f"""SELECT name, COUNT(*) AS cnt FROM behavior_events
               WHERE kind='browser' AND start_ts >= ? AND {clause}
               GROUP BY name ORDER BY cnt DESC LIMIT 10""",
            (since, *args),
        ).fetchall()
        n_commit = conn.execute(
            f"SELECT COUNT(*) AS c FROM behavior_events WHERE kind='git_commit' AND start_ts >= ? AND {clause}",
            (since, *args),
        ).fetchone()["c"]
        n_msg = conn.execute(
            f"SELECT COUNT(*) AS c FROM memories WHERE ts >= ? AND {clause}",
            (since, *args),
        ).fetchone()["c"]
        n_log = conn.execute(
            f"SELECT COUNT(*) AS c FROM work_log WHERE created_at >= ? AND {clause}",
            (since, *args),
        ).fetchone()["c"]
    finally:
        conn.close()
    return {
        "days": days,
        "messages": n_msg,
        "git_commits": n_commit,
        "work_logs": n_log,
        "top_apps": [{"name": a["name"], "hours": round((a["secs"] or 0) / 3600, 1)} for a in apps],
        "top_domains": [{"name": b["name"], "count": b["cnt"]} for b in browsers],
    }


@router.get("/stats/hourly")
async def stats_hourly(request: Request, days: int = 7) -> dict:
    """按小时分布：对话活跃时段 + 提交时段。"""
    owner = _owner_scope(request)
    clause, args = _user_scope(owner)
    days = max(1, min(int(days), 90))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = connect()
    try:
        rows = conn.execute(
            f"""SELECT CAST(strftime('%H', ts) AS INTEGER) AS h, COUNT(*) AS c
               FROM memories WHERE ts >= ? AND {clause} GROUP BY h""",
            (since, *args),
        ).fetchall()
    finally:
        conn.close()
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
