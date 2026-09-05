"""小月的自我状态：给 LLM 提供"她自己的处境"，而不是又一层对用户的分析。

现有八通道注入全部关于用户（用户情绪/画像/关切/事实），小月自己没有任何
连续状态——每次对话都是全新的她。这里补上最小的一份：

- 今天已经聊了多少轮 → 第一句话和聊了 20 轮之后的语气本该不同
- 距上次对话多久 → 久别（>3 天）可以有"好久没聊"的自然反应
- 最近是否被纠正过 → 会更谨慎一点

零 LLM、零新表（只读 memories / lessons），无内容时返回空串（不占 prompt）。
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.models.database import connect

TZ = ZoneInfo("Asia/Shanghai")

# 久别阈值（天）：超过即提示"好久没聊"
LONG_GAP_DAYS = 3
# 刚被纠正的时效（小时）：这段时间内她会更谨慎
RECENT_CORRECTION_HOURS = 24
# 熟络度档位（按今天已聊轮数）
WARMTH_TIERS = ((0, "刚开口"), (6, "聊开了"), (20, "聊了很久"))


def _parse_utc(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _warmth(turns: int) -> str:
    label = WARMTH_TIERS[0][1]
    for threshold, name in WARMTH_TIERS:
        if turns >= threshold:
            label = name
    return label


def get_self_state_injection(user_id: str | None = None) -> str:
    """小月自己的当前状态（一行，无内容返回空串）。

    轮数/久别按调用方用户隔离；"刚被纠正"只对主人生效——lessons 是主人
    单用户表，访客路径不读它（零跨用户泄漏）。
    """
    from app.core.memory import _user_scope, is_owner_user, normalize_user_id

    uid = normalize_user_id(user_id)
    is_owner = is_owner_user(uid)
    clause, args = _user_scope(uid)
    now = datetime.now(timezone.utc)
    day_start = datetime.now(TZ).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc).isoformat()

    conn = connect()
    try:
        turns = conn.execute(
            f"SELECT COUNT(*) AS c FROM memories WHERE sender='user' AND ts >= ? AND {clause}",
            (day_start, *args),
        ).fetchone()["c"]
        prev = conn.execute(
            f"SELECT ts FROM memories WHERE sender='user' AND ts < ? AND {clause} "
            "ORDER BY id DESC LIMIT 1",
            (day_start, *args),
        ).fetchone()
        last_lesson = conn.execute(
            f"SELECT created_at FROM lessons WHERE {clause} "
            "ORDER BY created_at DESC LIMIT 1",
            args,
        ).fetchone() if is_owner else None
    finally:
        conn.close()

    parts: list[str] = []
    if turns:
        parts.append(f"今天已经聊了 {turns} 轮（{_warmth(turns)}）")
    else:
        parts.append("今天还是第一句话")

    if prev:
        last = _parse_utc(prev["ts"])
        if last:
            gap_days = (now - last).days
            if gap_days >= LONG_GAP_DAYS:
                parts.append(f"距上次聊天已 {gap_days} 天（久别，可以自然提一句好久没聊）")

    if last_lesson:
        corrected = _parse_utc(last_lesson["created_at"])
        if corrected and (now - corrected) <= timedelta(hours=RECENT_CORRECTION_HOURS):
            parts.append("最近刚被纠正过（这轮更谨慎些，别急着下结论）")

    if not parts:
        return ""
    return "你自己的状态：" + "；".join(parts)
