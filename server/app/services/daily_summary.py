"""每日小结：每晚 22:00 生成"今天你做了什么"（3-5 行摘要）。

数据源：当天 memories 摘要 + behavior_events 统计 + work_log。
原则：事实由 SQL 算、解读由 LLM 做（与周报一致）。
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core import llm
from app.models.database import connect

# 按用户本地时区（北京时间）算"今天"，不用 UTC——凌晨 0-8 点会算错日子
TZ = ZoneInfo("Asia/Shanghai")


def _stale_concerns_text(days: int = 3) -> str:
    """超过 days 天未提及的关切（供小结提醒）。"""
    from app.services.concern_tracker import get_stale_concerns

    stale = get_stale_concerns(days=days)
    if not stale:
        return "（无）"
    return "、".join(f"{s['topic']}（{s['mention_count']} 次）" for s in stale)

DAILY_PROMPT = """你是用户的私人 AI 助手。基于今天的活动数据，写一份《今日小结》。
只输出 JSON：{{"content": "小结全文"}}

content 格式要求（Markdown，共 3-5 行）：
1. 第一行 `**<今日一句>**`——用一句话概括今天的主线（≤20 字，具体不空泛）
2. 中间 2-3 行 `- ` 列表：今天做了什么（对话主题/工作日志/应用使用），
   引用真实数据（如"调了 2h RAG"、"VSCode 4.5h"）；没数据的方面不写
3. 如有值得注意的观察，最后加一行 `💡 观察：...`；没有就不加
4. 如有超过 3 天未提及的关切话题，紧跟一行 `💭 以前的 XX 还没续上，要看看吗？`
要求：具体、简洁、不编造数据（没数据就说没有）。

今日对话摘要：
{summaries}

今日行为统计：
{stats}

今日工作日志：
{logs}

超过 3 天未提及的关切话题（如有，按上面第 4 条格式提醒）：
{stale_concerns}
"""


def get_latest_daily_summary() -> dict:
    """读取最近一份日报，不触发生成，也不调用 LLM。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT date, content, created_at FROM daily_summaries "
            "ORDER BY date DESC, id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else {}


async def run_daily_summary() -> dict:
    """生成今天的每日小结并存储。返回 {date, content} 或 {'skipped': True}。

    v0.4：日报是主人专属——对话摘要只读主人自己的（访客数据不混入）。
    """
    from app.core.memory import _user_scope, owner_user_id

    now = datetime.now(TZ)
    today = now.date().isoformat()
    day_start = datetime(now.year, now.month, now.day, tzinfo=TZ).isoformat()
    owner = owner_user_id()
    clause, uargs = _user_scope(owner)

    conn = connect()
    try:
        # 已有则不重复生成
        exists = conn.execute(
            "SELECT 1 FROM daily_summaries WHERE date=?", (today,)
        ).fetchone()
        if exists:
            return {"skipped": True, "reason": "今日小结已存在"}
        summaries = conn.execute(
            f"SELECT summary, topics FROM memories WHERE summary != '' AND summary != '__merged__' AND ts >= ? AND {clause} LIMIT 50",
            (day_start, *uargs),
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
    top_apps = ", ".join(f"{a['name']} {round((a['secs'] or 0) / 3600, 2)}h" for a in apps) or "无"
    stats_text = f"行为事件 {n_events} 条；应用时长 Top: {top_apps}"

    result = await llm.chat_json(
        "你是用户的私人 AI 助手，只输出 JSON。",
        DAILY_PROMPT.replace("{summaries}", summary_text)
        .replace("{stats}", stats_text)
        .replace("{logs}", log_text)
        .replace("{stale_concerns}", _stale_concerns_text()),
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
