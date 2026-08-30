"""unresolved 问题追踪：聊到一半被打断的话题，提醒续上。

检测（零 LLM）：
- 未解决信号："还没解决/卡住了/稍后再说/下次再说/没搞定/先放着" → 存 open issue
- 解决信号："解决了/搞定了/弄好了/做完了" → 最近 open issue 标记 resolved
注入：open issues 每次进 prompt；问候/小结提醒数量。
"""
from datetime import datetime, timezone

from app.models.database import connect

UNRESOLVED_PATTERNS = ("还没解决", "卡住了", "稍后再说", "下次再说", "没搞定", "先放着", "改天再")
RESOLVED_PATTERNS = ("解决了", "搞定了", "弄好了", "做完了", "已解决")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def detect_unresolved(text: str) -> bool:
    return any(p in text for p in UNRESOLVED_PATTERNS)


def detect_resolved(text: str) -> bool:
    return any(p in text for p in RESOLVED_PATTERNS)


def add_issue(topic: str, context: str = "") -> int:
    from app.services.sanitize import sanitize
    topic = sanitize(topic)
    context = sanitize(context)
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO unresolved_issues (topic, context, created_at) VALUES (?, ?, ?)",
            (topic[:120], context[:200], _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def resolve_latest(topic_hint: str = "") -> bool:
    """最近一个 open issue 标记 resolved。"""
    conn = connect()
    try:
        if topic_hint:
            cur = conn.execute(
                """UPDATE unresolved_issues SET status='resolved', resolved_at=?
                   WHERE status='open' AND topic LIKE ? AND id = (
                     SELECT id FROM unresolved_issues WHERE status='open' AND topic LIKE ?
                     ORDER BY id DESC LIMIT 1)""",
                (_now(), f"%{topic_hint}%", f"%{topic_hint}%"),
            )
        else:
            cur = conn.execute(
                """UPDATE unresolved_issues SET status='resolved', resolved_at=?
                   WHERE id = (SELECT id FROM unresolved_issues WHERE status='open'
                     ORDER BY id DESC LIMIT 1)""",
                (_now(),),
            )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_open_issues_injection(limit: int = 3) -> str:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT topic FROM unresolved_issues WHERE status='open' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return ""
    return "\n".join(f"- {r['topic']}" for r in rows)


def count_open() -> int:
    conn = connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM unresolved_issues WHERE status='open'"
        ).fetchone()["c"]
    finally:
        conn.close()
