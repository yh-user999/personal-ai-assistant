"""主动开口通道：让她能"先找你"，而不是只在被问时才存在。

补的是一个真实断点：每晚 22:00 的《今日小结》和 get_stale_concerns 算出的
"搁置话题"全都只写进库，QQ 通道此前只推到期提醒（send_private_msg 全项目
仅 2 处调用，都在 push_reminders 里）——算出来没有出口，等于白算。

行为边界（设计上的硬约束，不靠自觉）：
- 默认关闭：INITIATIVE_ENABLED=false，用户确认体验后才开
- 每日最多 1 条
- 夜间静默：22:00-次日 8:00 不推（到期提醒不受此限，走原通道）
- 无回应自动降频：连续 3 次推送没得到回话，降为每周最多 1 条（她会识趣）
- 同一搁置话题只主动问一次（问两遍就从关心变催促）

可靠性沿用 qq_push 语义：推送成功才入账，失败不入账、下轮重试。
"""
import logging
from datetime import datetime, timedelta, timezone

from openai import OpenAIError

from app.config import settings
from app.models.database import connect

logger = logging.getLogger("assistant.initiative")

from app.common.timeutil import TZ

DAILY_MAX = 1                # 每日最多主动开口条数
NO_RESPONSE_STREAK = 3       # 连续多少条无回应即降频
COOLDOWN_DAYS_WHEN_IGNORED = 7   # 降频后的最小间隔（天）
CONCERN_STALE_DAYS = 3       # 关切搁置多少天才值得问
CONCERN_MIN_INTERVAL_DAYS = 7  # 关切类追问彼此至少隔多久


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def in_quiet_hours(now_local: datetime | None = None) -> bool:
    """是否处于夜间静默时段（默认本地 23:00-08:00，见 .env 的两个 QUIET 项）。

    start == end 视为不静默（全天可推），而不是全天静默——配错也不至于
    让通道整体失效。
    """
    h = (now_local or datetime.now(TZ)).hour
    start = settings.initiative_quiet_start % 24
    end = settings.initiative_quiet_end % 24
    if start == end:
        return False
    if start < end:            # 例如 0-8
        return start <= h < end
    return h >= start or h < end  # 跨零点，例如 23-8


# ── 台账 ──────────────────────────────────────────────────

def log_sent(kind: str, content: str, topic: str = "") -> int:
    """推送成功后入账（失败不入账，才能下轮重试）。"""
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO initiative_log (kind, content, topic, sent_at, responded) "
            "VALUES (?, ?, ?, ?, 0)",
            (kind, content[:300], topic[:80], _now_utc().isoformat()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def mark_responded() -> int:
    """用户回话了 → 把最近一条未回应的主动消息标记为已回应。

    由聊天入口调用（她开口后你说了话，就算回应）。返回标记条数（0/1）。
    """
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id FROM initiative_log WHERE responded = 0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return 0
        conn.execute("UPDATE initiative_log SET responded = 1 WHERE id = ?", (row["id"],))
        conn.commit()
        return 1
    finally:
        conn.close()


def _sent_today() -> int:
    day_start = datetime.now(TZ).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc).isoformat()
    conn = connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM initiative_log WHERE sent_at >= ?", (day_start,)
        ).fetchone()["c"]
    finally:
        conn.close()


def _recent(limit: int = 10) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT kind, topic, sent_at, responded FROM initiative_log "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def is_ignored() -> bool:
    """连续 NO_RESPONSE_STREAK 条主动消息都没被回应 → 该降频了。"""
    streak = 0
    for r in _recent(NO_RESPONSE_STREAK):
        if r["responded"]:
            return False
        streak += 1
    return streak >= NO_RESPONSE_STREAK


def should_speak(now_local: datetime | None = None) -> tuple[bool, str]:
    """能否主动开口。返回 (允许, 不允许时的原因)。"""
    if not settings.initiative_enabled:
        return False, "主动开口未启用（INITIATIVE_ENABLED=false）"
    if not (settings.qq_push_url and settings.qq_admin_id):
        return False, "QQ 通道未配置"
    if in_quiet_hours(now_local):
        return False, "夜间静默时段"
    if _sent_today() >= DAILY_MAX:
        return False, f"今日已达上限（{DAILY_MAX} 条）"
    if is_ignored():
        recent = _recent(1)
        last = _parse(recent[0]["sent_at"]) if recent else None
        if last and (_now_utc() - last) < timedelta(days=COOLDOWN_DAYS_WHEN_IGNORED):
            return False, f"连续无回应已降频（{COOLDOWN_DAYS_WHEN_IGNORED} 天最多 1 条）"
    return True, ""


# ── 内容生成 ──────────────────────────────────────────────

DAILY_LINE_PROMPT = """你是「小月」，用户的私人 AI 助手。下面是今天的小结原文。
把它压成**一句**主动发给用户的话（微信/QQ 口气），只输出 JSON：{{"line": "那句话"}}

要求：
- 就是一句，≤40 字，纯文本，不要 Markdown、不要列表、不要 emoji 堆叠
- 提一个今天的具体事实（时长/主题/进展），不要泛泛地说"今天辛苦了"
- 像朋友随口发的消息，不是通报；可以带一句轻的关心或收尾问句
- 不编造小结里没有的事

今日小结原文：
{summary}
"""


async def build_daily_line(summary: str) -> str:
    """今日小结 → 一句最像人说的话。LLM 失败/空返回时返回空串（不推）。"""
    from app.core import llm

    if not summary.strip():
        return ""
    try:
        result = await llm.chat_json(
            "你是「小月」，只输出 JSON。",
            DAILY_LINE_PROMPT.replace("{summary}", summary[:1500]),
        )
    except (OpenAIError, TimeoutError, RuntimeError) as e:
        logger.warning("主动开口文案生成失败: %s", e)
        return ""
    line = (result.get("line") or "").strip()
    return line[:120]


def pick_stale_concern() -> dict | None:
    """挑一个值得续上的搁置话题（最多 1 个，且同话题只问一次）。

    还要看 concern 类追问彼此的间隔——一周内已问过别的话题就先不问。
    """
    from app.services.concern_tracker import get_stale_concerns

    for r in _recent(10):
        if r["kind"] != "concern":
            continue
        sent = _parse(r["sent_at"])
        if sent and (_now_utc() - sent) < timedelta(days=CONCERN_MIN_INTERVAL_DAYS):
            return None
        break
    stale = get_stale_concerns(days=CONCERN_STALE_DAYS)
    return stale[0] if stale else None


# ── 推送 ──────────────────────────────────────────────────

async def _push(text: str) -> bool:
    """发一条 QQ 私聊，返回是否确认送达。

    走 qq_push.send_private 单一出口——原先本模块自建 httpx.AsyncClient
    且重复实现"成功"判据，连接不复用。
    """
    from app.services.qq_push import send_private

    return await send_private(text)


async def run_initiative() -> dict:
    """主动开口一轮：优先今日小结一句，其次搁置话题续上。

    返回 {"sent": kind, "content": ...} 或 {"skipped": True, "reason": ...}。
    推送成功才入账——失败下轮重试，与 qq_push 一致。
    """
    ok, reason = should_speak()
    if not ok:
        return {"skipped": True, "reason": reason}

    today = datetime.now(TZ).date().isoformat()
    conn = connect()
    try:
        row = conn.execute(
            "SELECT content FROM daily_summaries WHERE date = ?", (today,)
        ).fetchone()
    finally:
        conn.close()

    if row and row["content"]:
        line = await build_daily_line(row["content"])
        if line and await _push(line):
            log_sent("daily", line)
            return {"sent": "daily", "content": line}

    concern = pick_stale_concern()
    if concern:
        from app.services.concern_tracker import mark_asked

        line = f"上次说的{concern['topic']}后来怎么样了？"
        if await _push(line):
            log_sent("concern", line, topic=concern["topic"])
            mark_asked(concern["topic"])
            return {"sent": "concern", "content": line}

    return {"skipped": True, "reason": "无可说的内容或推送失败"}
