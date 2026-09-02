"""行为统计接口：供 Web 仪表盘 / 桌面端查询。"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from app.models.database import connect

router = APIRouter()


@router.get("/stats/summary")
async def stats_summary(days: int = 7) -> dict:
    """总览：对话数、提交数、应用时长 Top、浏览域名 Top、日志数。"""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = connect()
    try:
        apps = conn.execute(
            """SELECT name, SUM(CAST(julianday(end_ts)-julianday(start_ts) AS REAL)*86400) AS secs
               FROM behavior_events WHERE kind='app_usage' AND start_ts >= ?
               GROUP BY name ORDER BY secs DESC LIMIT 10""",
            (since,),
        ).fetchall()
        browsers = conn.execute(
            """SELECT name, COUNT(*) AS cnt FROM behavior_events
               WHERE kind='browser' AND start_ts >= ? GROUP BY name ORDER BY cnt DESC LIMIT 10""",
            (since,),
        ).fetchall()
        n_commit = conn.execute(
            "SELECT COUNT(*) AS c FROM behavior_events WHERE kind='git_commit' AND start_ts >= ?",
            (since,),
        ).fetchone()["c"]
        n_msg = conn.execute("SELECT COUNT(*) AS c FROM memories WHERE ts >= ?", (since,)).fetchone()["c"]
        n_log = conn.execute("SELECT COUNT(*) AS c FROM work_log WHERE created_at >= ?", (since,)).fetchone()["c"]
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
async def stats_hourly(days: int = 7) -> dict:
    """按小时分布：对话活跃时段 + 提交时段。"""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT CAST(strftime('%H', ts) AS INTEGER) AS h, COUNT(*) AS c
               FROM memories WHERE ts >= ? GROUP BY h""",
            (since,),
        ).fetchall()
    finally:
        conn.close()
    hours = [0] * 24
    for r in rows:
        hours[r["h"]] = r["c"]
    return {"hours": hours}


@router.get("/stats/cost")
async def stats_cost(days: int = 7) -> dict:
    """成本画像（P4）：LLM token 累计 + 检索决策轨迹聚合。"""
    import asyncio

    from app.services.cost_report import cost_report

    return await asyncio.to_thread(cost_report, days)
