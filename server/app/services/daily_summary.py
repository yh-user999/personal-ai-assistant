"""每日小结：每晚 22:00 生成"今天你做了什么"（3-5 行摘要）。

数据源：当天 memories 摘要 + behavior_events 统计 + work_log。
原则：事实由 SQL 算、解读由 LLM 做（与周报一致）。
"""
from datetime import datetime, timedelta, timezone

from app.core import llm
from app.models.database import connect

DAILY_PROMPT = """你是用户的私人 AI 助手。基于今天的活动数据，写一份 3-5 行的《今日小结》：
1. 今天做了什么（对话主题/工作日志/应用使用）
2. 一个值得注意的观察（如有）
要求：具体、简洁、不编造数据（没数据就说没有）。

今日对话摘要：
{summaries}

今日行为统计：
{stats}

今日工作日志：
{logs}
"""


async def run_daily_summary() -> dict:
    """生成今天的每日小结并存储。返回 {date, content} 或 {'skipped': True}。"""
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).isoformat()

    conn = connect()
    try:
        # 已有则不重复生成
        exists = conn.execute(
            "SELECT 1 FROM daily_summaries WHERE date=?", (today,)
        ).fetchone()
        if exists:
            return {"skipped": True, "reason": "今日小结已存在"}
        summaries = conn.execute(
            "SELECT summary, topics FROM memories WHERE summary != '' AND summary != '__merged__' AND ts >= ? LIMIT 50",
            (day_start,),
        ).fetchall()
        logs = conn.execute(
            "SELECT time_range, content FROM work_log WHERE date=? ORDER BY id LIMIT 20", (today,)
        ).fetchall()
        # 行为统计：事件数 + 应用时长 top
        n_events = conn.execute(
            "SELECT COUNT(*) AS c FROM behavior_events WHERE start_ts >= ?", (day_start,)
        ).fetchone()["c"]
        apps = conn.execute(
            """SELECT name, SUM(CAST(julianday(end_ts)-julianday(start_ts) AS REAL)*86400) AS secs
               FROM behavior_events WHERE kind='app_usage' AND start_ts >= ?
               GROUP BY name ORDER BY secs DESC LIMIT 5""",
            (day_start,),
        ).fetchall()
    finally:
        conn.close()

    # 完全没数据的一天：跳过（不烧 LLM）
    if n_events == 0 and not summaries and not logs:
        return {"skipped": True, "reason": "今日无任何活动数据"}

    summary_text = "\n".join(
        f"[{r['topics']}] {r['summary']}" for r in summaries[:20]
    ) or "（无对话）"
    log_text = "\n".join(f"{r['time_range']} {r['content']}" for r in logs) or "（无日志）"
    stats_text = (
        f"行为事件 {n_events} 条；应用时长 Top: "
        + ", ".join(f"{a['name']} {round((a['secs'] or 0) / 3600, 2)}h" for a in apps)
    )

    result = await llm.chat_json(
        "你是用户的私人 AI 助手，只输出 JSON。",
        DAILY_PROMPT.replace("{summaries}", summary_text)
        .replace("{stats}", stats_text)
        .replace("{logs}", log_text),
    )
    # 输出格式 { "content": "..." }；容错取 summary 字段
    content = result.get("content") or result.get("summary") or ""
    if not content:
        return {"skipped": True, "reason": "LLM 未返回内容"}

    conn = connect()
    try:
        conn.execute(
            "INSERT INTO daily_summaries (date, content, created_at) VALUES (?, ?, ?)",
            (today, content, now.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"date": today, "content": content}
