"""行为分析：纯 SQL 统计（无 LLM）+ 淘汰任务。

统计维度：
- 应用使用时长 Top / 按时段分布
- 浏览器域名话题分布
- git 提交频率/时段
- 工作日志汇总
"""
import json
from datetime import datetime, timedelta, timezone

from app.models.database import connect


def weekly_stats(days: int = 7) -> dict:
    """周报用的统计摘要（键值对，直接给 LLM）。"""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = connect()
    try:
        # 应用使用时长 Top5
        apps = conn.execute(
            """SELECT name, SUM(CAST(julianday(end_ts) - julianday(start_ts) AS REAL) * 86400) AS secs
               FROM behavior_events WHERE kind='app_usage' AND start_ts >= ?
               GROUP BY name ORDER BY secs DESC LIMIT 5""",
            (since,),
        ).fetchall()
        # 浏览器域名 Top5
        browsers = conn.execute(
            """SELECT name, COUNT(*) AS cnt FROM behavior_events
               WHERE kind='browser' AND start_ts >= ? GROUP BY name ORDER BY cnt DESC LIMIT 5""",
            (since,),
        ).fetchall()
        # git 提交数
        commits = conn.execute(
            "SELECT COUNT(*) AS cnt FROM behavior_events WHERE kind='git_commit' AND start_ts >= ?",
            (since,),
        ).fetchone()["cnt"]
        # 对话条数
        msgs = conn.execute(
            "SELECT COUNT(*) AS cnt FROM memories WHERE ts >= ?", (since,)
        ).fetchone()["cnt"]
    finally:
        conn.close()

    return {
        "本周对话条数": msgs,
        "本周git提交数": commits,
        "应用时长Top5": json.dumps(
            [{"app": a["name"], "hours": round((a["secs"] or 0) / 3600, 1)} for a in apps],
            ensure_ascii=False,
        ),
        "浏览域名Top5": json.dumps(
            [{"domain": b["name"], "次数": b["cnt"]} for b in browsers], ensure_ascii=False
        ),
    }


async def evict_stale() -> dict:
    """淘汰：noise 类事件 7 天删除；超 30 天未再引用的低 importance 记忆删除。"""
    conn = connect()
    try:
        noise_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        n1 = conn.execute(
            "DELETE FROM behavior_events WHERE kind='manual' AND start_ts < ?", (noise_cutoff,)
        ).rowcount
        chat_cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        n2 = conn.execute(
            "DELETE FROM memories WHERE ts < ? AND importance < 1.0", (chat_cutoff,)
        ).rowcount
        conn.commit()
        return {"deleted_noise": n1, "deleted_chat": n2}
    finally:
        conn.close()
