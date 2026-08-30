"""术语学习：用户问过的技术名词自动建档，之后解释口径一致。

v1 零额外 LLM 成本：检测定义型问题 → 术语=问题名词、解释=AI 回复（截断）。
注入：消息含已知术语时，注入其解释。
"""
import re

from app.models.database import connect

_DEFINITION_RE = re.compile(r"(?:什么是|介绍一下|讲讲|解释下|解释一下)\s*(.{1,50})")


def detect_definition(text: str) -> str | None:
    """检测"什么是 X"类问题，返回术语 X（无则 None）。"""
    m = _DEFINITION_RE.search(text)
    if not m:
        return None
    term = re.sub(r"[？?。.\s]+$", "", m.group(1)).strip()
    return term[:40] if term else None


def save_term(term: str, explanation: str) -> int:
    """存术语（已存在则更新解释并刷新时间，入库前统一脱敏）。"""
    from datetime import datetime, timezone
    from app.services.sanitize import sanitize
    term = sanitize(term)
    explanation = sanitize(explanation)
    now = datetime.now(timezone.utc).isoformat()
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO jargon_terms (term, explanation, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(term) DO UPDATE SET
                 explanation = excluded.explanation,
                 created_at = excluded.created_at""",
            (term, explanation[:500], now),
        )
        conn.commit()
    finally:
        conn.close()
    return 1


def get_jargon_injection(message: str, limit: int = 3) -> str:
    """消息中含已知术语 → 注入其解释（最多 limit 条）。"""
    conn = connect()
    try:
        rows = conn.execute("SELECT term, explanation FROM jargon_terms").fetchall()
    finally:
        conn.close()
    hits = [(r["term"], r["explanation"]) for r in rows if r["term"] in message]
    if not hits:
        return ""
    parts = [f"<术语> {t}：{e[:150]}" for t, e in hits[:limit]]
    return "\n".join(parts)
