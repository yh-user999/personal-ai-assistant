"""每周学习反思：拉本周记忆/日志/行为统计 → LLM 生成反思报告 → 归档 + 可推送。"""
from datetime import datetime, timedelta, timezone

from app.core import llm
from app.models.database import connect

REFLECT_PROMPT = """你是用户的私人 AI 助手，每周生成一份《学习进度反思》报告。
基于以下材料，输出：
{{
  "report": "完整报告 Markdown，包含：本周成长点、行为模式变化、项目进度核对、下周建议(1-3条)"
}}
要求：具体、可执行、引用真实数据；不要空话套话。

本周对话摘要：
{summaries}

本周行为统计：
{stats}

本周工作日志：
{logs}

当前画像：
{profile}
"""


async def run_weekly_reflect() -> dict:
    """生成本周反思并归档。返回 {week, report}。"""
    now = datetime.now(timezone.utc)
    week = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
    since = (now - timedelta(days=7)).isoformat()

    conn = connect()
    try:
        summaries = conn.execute(
            "SELECT summary, topics, ts FROM memories WHERE summary != '' AND summary != '__merged__' AND ts >= ? LIMIT 200",
            (since,),
        ).fetchall()
        logs = conn.execute(
            "SELECT date, time_range, content, project FROM work_log WHERE created_at >= ?",
            (since,),
        ).fetchall()
        profile = conn.execute("SELECT dimension, value FROM profile").fetchall()
    finally:
        conn.close()

    from app.services.analyzer import weekly_stats
    stats = weekly_stats(days=7)

    summary_text = "\n".join(
        f"[{r['ts'][:10]}] {r['summary']} topics={r['topics']}" for r in summaries[:50]
    ) or "（本周暂无对话）"
    log_text = "\n".join(f"{r['date']} {r['time_range']} {r['content']} [{r['project']}]" for r in logs) or "（无）"
    profile_text = "\n".join(f"[{r['dimension']}] {r['value']}" for r in profile) or "（空）"
    stats_text = "\n".join(f"{k}: {v}" for k, v in stats.items())

    result = await llm.chat_json(
        "你是用户的私人 AI 助手，只输出 JSON。",
        REFLECT_PROMPT
        .replace("{summaries}", summary_text)
        .replace("{stats}", stats_text)
        .replace("{logs}", log_text)
        .replace("{profile}", profile_text),
    )
    report = result.get("report", "")

    conn = connect()
    try:
        conn.execute(
            """INSERT INTO weekly_reports (week, content, stats, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(week) DO UPDATE SET content=excluded.content, stats=excluded.stats""",
            (week, report, str(stats), now.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"week": week, "report": report}
