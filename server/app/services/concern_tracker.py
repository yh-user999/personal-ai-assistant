"""关切追踪：维护"用户最近在意的话题"，支持主动提醒。

数据流：摘要整合（consolidation）产出 topics → 更新关切表；
聊天时注入当前关切；每日小结时检测"3 天没提的关切"并提醒。
"""
from datetime import datetime, timedelta, timezone

from app.models.database import connect


def upsert_concerns(topics: list[str]) -> int:
    """话题提及：存在则计数+1 并刷新时间，否则新建。返回更新的条数。"""
    if not topics:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    conn = connect()
    try:
        for t in topics:
            t = (t or "").strip()[:50]
            if not t:
                continue
            conn.execute(
                """INSERT INTO concerns (topic, mention_count, last_mentioned_at)
                   VALUES (?, 1, ?)
                   ON CONFLICT(topic) DO UPDATE SET
                     mention_count = mention_count + 1,
                     last_mentioned_at = excluded.last_mentioned_at""",
                (t, now),
            )
        conn.commit()
    finally:
        conn.close()
    return len(topics)


def get_concerns_injection(limit: int = 6) -> str:
    """当前关切注入：最近提及的话题（含次数）。"""
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT topic, mention_count, last_mentioned_at FROM concerns
               ORDER BY last_mentioned_at DESC LIMIT ?""",
            (limit,),
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
        except Exception:
            pass
        parts.append(f"- {r['topic']}（提到 {r['mention_count']} 次{days}）")
    return "\n".join(parts)


def get_stale_concerns(days: int = 3) -> list[dict]:
    """超过 days 天没再提及、且曾经至少提过 2 次的关切（需要提醒的）。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT topic, mention_count, last_mentioned_at FROM concerns
               WHERE mention_count >= 2 AND last_mentioned_at < ?
               ORDER BY last_mentioned_at ASC""",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
