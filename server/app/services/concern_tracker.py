"""关切追踪：维护"用户最近在意的话题"，支持主动提醒。

数据流：摘要整合（consolidation）产出 topics → 更新关切表；
聊天时注入当前关切；每日小结时检测"3 天没提的关切"并提醒。
"""
from datetime import datetime, timedelta, timezone

from app.models.database import connect


def upsert_concerns(topics: list[str], user_id: str | None = None) -> int:
    """话题提及：存在则计数+1 并刷新时间，否则新建。返回更新的条数。"""
    if not topics:
        return 0
    from app.core.memory import normalize_user_id
    from app.services.sanitize import sanitize

    uid = normalize_user_id(user_id)
    topics = [sanitize(t or "") for t in topics]
    now = datetime.now(timezone.utc).isoformat()
    conn = connect()
    try:
        for t in topics:
            t = (t or "").strip()[:50]
            if not t:
                continue
            conn.execute(
                """INSERT INTO concerns (user_id, topic, mention_count, last_mentioned_at)
                   VALUES (?, ?, 1, ?)
                   ON CONFLICT(user_id, topic) DO UPDATE SET
                     mention_count = mention_count + 1,
                     last_mentioned_at = excluded.last_mentioned_at""",
                (uid, t, now),
            )
        conn.commit()
    finally:
        conn.close()
    return len(topics)


def get_concerns_injection(limit: int = 4, user_id: str | None = None) -> str:
    """当前关切注入：最近提及的话题（含次数，v0.4.1 收紧为 4 条）。"""
    from app.core.memory import normalize_user_id

    uid = normalize_user_id(user_id)
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT topic, mention_count, last_mentioned_at FROM concerns
               WHERE user_id = ? ORDER BY last_mentioned_at DESC LIMIT ?""",
            (uid, limit),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return ""
    parts = []
    for r in rows:
        days = ""
        try:
            last = datetime.fromisoformat(r["last_mentioned_at"])
            days = f"，最近提及 {(datetime.now(timezone.utc) - last).days} 天前"
        except (TypeError, ValueError):
            days = ""
        parts.append(f"- {r['topic']}（提到 {r['mention_count']} 次{days}）")
    return "\n".join(parts)


def get_stale_concerns(days: int = 3, user_id: str | None = None) -> list[dict]:
    """超过 days 天没再提及、且曾经至少提过 2 次的关切（需要提醒的）。

    已主动追问过的（asked_at 非空）排除：追问两遍就从关心变成催促。
    """
    from app.core.memory import normalize_user_id

    uid = normalize_user_id(user_id)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT topic, mention_count, last_mentioned_at FROM concerns
               WHERE user_id = ? AND mention_count >= 2 AND last_mentioned_at < ?
                 AND (asked_at IS NULL OR asked_at = '')
               ORDER BY last_mentioned_at ASC""",
            (uid, cutoff),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def mark_asked(topic: str, user_id: str | None = None) -> None:
    """记录"已主动追问过这个话题"（幂等；同话题不再问第二次）。"""
    from app.core.memory import normalize_user_id

    uid = normalize_user_id(user_id)
    conn = connect()
    try:
        conn.execute(
            "UPDATE concerns SET asked_at = ? WHERE user_id = ? AND topic = ?",
            (datetime.now(timezone.utc).isoformat(), uid, topic),
        )
        conn.commit()
    finally:
        conn.close()
