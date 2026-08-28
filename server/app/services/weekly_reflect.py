"""每周学习反思：拉本周记忆/日志/行为统计 → LLM 生成反思报告 → 归档 + 可推送。"""
from datetime import datetime, timedelta, timezone

from app.core import llm
from app.models.database import connect

REFLECT_PROMPT = """你是用户的私人 AI 助手，每周生成一份《学习进度反思》报告。
基于以下材料，输出：
{{
  "report": "完整报告 Markdown"
}}
要求：具体、可执行、引用真实数据；不要空话套话；数据没有就写"本周无记录"，不要编造。

报告必须严格遵循以下结构（Markdown，二级标题开头，可用 emoji 但不堆砌）：

## 📊 本周概览
先用 2-3 句话给本周一个总评（在忙什么、状态如何），再列 4-6 个关键数字
（对话条数、git 提交、工作日志条数、被纠正次数、主力应用/网站——直接引用下方统计，
格式如 `- 对话 **128** 条（环比参考无则不写环比）`）。

## 🌱 成长与变化
本周学到什么、哪些行为模式在变、被纠正后改进了什么。每条一行 `- `，
**必须引用本周真实对话/日志中的具体事例**，没有就写"本周记录较少，暂无明显变化"。

## ✅ 项目进度
对照工作日志逐项目列进度（`- 项目名：做了什么 / 推进到哪`）；本周没动
的项目明确写"未推进"。没填任何工作日志时写"本周未填写工作日志"。

## 💡 下周建议
1-3 条，每条**具体到可执行**（例："把 XX 的部署脚本补进 work_log，方便周报核对进度"），
禁止"继续保持""再接再厉"这类空话。

本周对话摘要：
{summaries}

本周行为统计：
{stats}

本周工作日志：
{logs}

当前画像：
{profile}
"""


def _enforce_real_numbers(report: str, stats: dict) -> str:
    """概览区数字兜底：在报告末尾附真实统计块，防止 LLM 抄错数。

    不改动 LLM 正文（改写易伤排版），而是追加一个"📊 真实数据核对"折叠段，
    读者可直接对账——数据由 SQL 计算，比 LLM 转述可信。
    """
    lines = ["", "---", "📊 **真实数据核对**（SQL 直算，如有出入以此为准）"]
    for k, v in stats.items():
        lines.append(f"- {k}：{v}")
    return report.rstrip() + "\n" + "\n".join(lines)


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
    from app.services.self_reflect import count_lessons_since
    stats = weekly_stats(days=7)
    stats["本周被纠正次数"] = count_lessons_since(since)

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
    report = (result.get("report") or "").strip()
    if report:
        # 数据准确性兜底：LLM 可能抄错数字，概览区的关键数字直接用真实统计替换
        report = _enforce_real_numbers(report, stats)

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
