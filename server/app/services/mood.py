"""情绪/语气感知（第 6.23 课：零成本规则检测；第 6.27 课：情绪记忆层 + 反馈闭环）。

- detect_mood_name：规则命中返回情绪名（疲惫/着急/烦躁/开心/低落）
- detect_mood：返回当句回复风格指引（第 6.23 课）
- 第 6.27 课 A 档：mood_log 情绪账——每条用户消息的情绪先入账，
  get_today_injection() 把"今日情绪曲线"注入 prompt（小月整天记得你今天不顺）
- 第 6.27 课 B 档：get_streak_injection()——连续 2+ 轮负面情绪（烦躁/疲惫/低落）
  自动降级为倾听模式（少建议、多共情、主动问要不要换个方式）
"""
import re
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.models.database import connect

TZ = ZoneInfo("Asia/Shanghai")

# (情绪名, (信号正则...), 回复指引)
MOOD_PATTERNS: list[tuple[str, tuple[str, ...], str]] = [
    ("疲惫", (
        r"累死|好累|累[了啦]|太困|好困|熬夜|没睡|不想动|先歇|休息|收工|下班",
    ), "用户疲惫：回复简短，少提建议、别安排新任务，多点体谅，结尾可提醒休息"),
    ("着急", (
        r"快点|赶紧|马上要|来不及|紧急|在线等|急用|抓紧",
    ), "用户着急：开门见山直接给答案，少铺垫少寒暄"),
    ("烦躁", (
        r"烦死|好烦|烦人|无语|气死|真服了|火大|来气|真受不了",
    ), "用户情绪不佳：先共情一句再给方案，避免说教和反驳"),
    ("开心", (
        r"哈哈+|太好了|搞定|成功了|nice|666|太爽|好耶|拿下",
    ), "用户心情好：可以适当轻松互动，呼应一下喜悦"),
    ("低落", (
        r"难过|失落|沮丧|想哭|没意思|绝望|心累|emo",
    ), "用户情绪低落：温和鼓励、认真倾听，不灌鸡汤不敷衍"),
]

_GUIDANCE = {name: guidance for name, _, guidance in MOOD_PATTERNS}

# 负面情绪（B 档：连续命中触发降级）
NEGATIVE_MOODS = ("烦躁", "疲惫", "低落")

# 连续负面轮数阈值：达到即降级为倾听模式
STREAK_THRESHOLD = 2
STREAK_LOOKBACK = 10      # 最多回看最近多少条情绪记录
STREAK_FRESH_HOURS = 2    # 连击时效：最近一次负面情绪超过 2 小时即过期（不跨天传染）


def _user_scope(user_id: str | None) -> tuple[str, str, tuple]:
    from app.core.memory import _user_scope as build_scope
    from app.core.memory import normalize_user_id

    uid = normalize_user_id(user_id)
    clause, args = build_scope(uid, col="user_id")
    return uid, clause, args


def detect_mood_name(msg: str) -> str | None:
    """规则命中返回情绪名；无命中返回 None。"""
    for name, patterns, _ in MOOD_PATTERNS:
        for p in patterns:
            if re.search(p, msg):
                return name
    return None


def detect_mood(msg: str) -> str:
    """返回当句回复风格指引文本；无命中返回空串。"""
    name = detect_mood_name(msg)
    return _GUIDANCE.get(name, "")


# ── 第 6.27 课：情绪记忆层（A 档）──────────────────────────

def record_mood(mood_name: str, msg: str, user_id: str | None = None) -> int:
    """入账一条情绪记录（UTC 存储，东八区解读）。"""
    uid, _, _ = _user_scope(user_id)
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO mood_log (user_id, mood, snippet, created_at) VALUES (?, ?, ?, ?)",
            (uid, mood_name, msg.strip()[:80], _utc_now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _utc_now() -> str:
    from app.common.timeutil import utc_str
    return utc_str()


def _row_local(row) -> datetime:
    dt = datetime.fromisoformat(row["created_at"])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(TZ)


def get_today_injection(user_id: str | None = None) -> str:
    """今日（东八区）情绪曲线：如 "今日情绪：烦躁×2、疲惫×1（下午）"。无记录返回空串。"""
    _, clause, user_args = _user_scope(user_id)
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT mood, created_at FROM mood_log WHERE {clause} ORDER BY id DESC LIMIT 200",
            user_args,
        ).fetchall()
    finally:
        conn.close()
    today = datetime.now(TZ).date()
    counts: Counter = Counter()
    last_hour = None
    for r in rows:
        dt = _row_local(r)
        if dt.date() != today:
            continue
        counts[r["mood"]] += 1
        if last_hour is None:
            last_hour = dt.hour
    if not counts:
        return ""
    period = (
        "凌晨" if last_hour < 5 else "早上" if last_hour < 9 else "上午" if last_hour < 12
        else "中午" if last_hour < 13 else "下午" if last_hour < 18 else "晚上"
    )
    parts = "、".join(f"{name}×{n}" for name, n in counts.most_common())
    return f"今日情绪：{parts}（最近一次在{period}）。延续今日情绪语境回复，别当作新的一天重新寒暄"


# ── 第 6.27 课：反馈闭环（B 档）────────────────────────────

def get_streak_injection(user_id: str | None = None) -> str:
    """最近情绪连击：连续 2+ 轮负面情绪 → 降级为倾听模式指引。否则空串。

    连击有时效：最近一条负面记录超过 STREAK_FRESH_HOURS 即视为过期——
    昨天的烦躁不该让今天一整天都在倾听模式里。
    """
    _, clause, user_args = _user_scope(user_id)
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT mood, created_at FROM mood_log WHERE {clause} ORDER BY id DESC LIMIT ?",
            (*user_args, STREAK_LOOKBACK),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return ""
    newest = _row_local(rows[0])
    if (datetime.now(TZ) - newest).total_seconds() > STREAK_FRESH_HOURS * 3600:
        return ""
    streak = 0
    for r in rows:
        if r["mood"] in NEGATIVE_MOODS:
            streak += 1
        else:
            break
    if streak < STREAK_THRESHOLD:
        return ""
    return (
        f"用户已连续 {streak} 轮情绪不佳（负面连击）：本轮进入倾听模式——"
        "少提建议、不安排新任务、不追问细节，先接住情绪，"
        "结尾可以轻轻问一句「需要我换个方式帮你吗」"
    )


# ── 隔日跟进：真人会"记得昨天" ──────────────────────────────

YESTERDAY_NEGATIVE_THRESHOLD = 2  # 昨天负面情绪达到几条才值得跟进


def get_yesterday_followup(user_id: str | None = None) -> str:
    """昨天情绪不佳且今天还没聊过 → 提示轻轻关心一句（问过就不再提）。

    情绪连击只管当轮（STREAK_FRESH_HOURS=2 后清零），但真人不是这样：
    昨天你很烦，今天他会先问一句"昨天那事顺了吗"。
    "今天首次对话"的判定用 mood_log 本身——今天已有情绪记录说明已经聊过，
    这句关心的时机就过了（不在对话中途突然回头问昨天）。
    零 LLM、零新表。
    """
    _, clause, user_args = _user_scope(user_id)
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT mood, created_at FROM mood_log WHERE {clause} ORDER BY id DESC LIMIT 200",
            user_args,
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return ""
    today = datetime.now(TZ).date()
    yesterday = today - timedelta(days=1)
    counts: Counter = Counter()
    for r in rows:
        d = _row_local(r).date()
        if d == today:
            return ""  # 今天已经聊过了，跟进时机已过
        if d == yesterday and r["mood"] in NEGATIVE_MOODS:
            counts[r["mood"]] += 1
    if sum(counts.values()) < YESTERDAY_NEGATIVE_THRESHOLD:
        return ""
    parts = "、".join(f"{name}×{n}" for name, n in counts.most_common())
    return (
        f"用户昨天情绪不佳（{parts}）：今天第一次说话，可以轻轻关心一句"
        "「昨天那事后来顺了吗」，问过就别再提，也别追问细节"
    )


def get_state_injection(user_id: str | None = None) -> str:
    """今日曲线 + 连击降级 + 隔日跟进，合并成一条注入（无则空串）。"""
    if user_id is None:
        providers = (
            get_today_injection(),
            get_streak_injection(),
            get_yesterday_followup(),
        )
    else:
        providers = (
            get_today_injection(user_id=user_id),
            get_streak_injection(user_id=user_id),
            get_yesterday_followup(user_id=user_id),
        )
    return "\n".join(p for p in providers if p)


# ── 第 6.28 课 C1：情绪周报统计 ────────────────────────────

def get_weekly_stats(days: int = 7, user_id: str | None = None) -> dict:
    """近 N 天（东八区）情绪聚合：总数 / 分布 / 负面话题 top5 / 高峰时段（3 小时桶）。"""
    _, clause, user_args = _user_scope(user_id)
    conn = connect()
    try:
        # 时间下界在 SQL 层过滤（原 LIMIT 500 在重度使用时会静默漏掉旧记录）
        cutoff_utc = (datetime.now(TZ) - timedelta(days=days)).astimezone(ZoneInfo("UTC")).isoformat()
        rows = conn.execute(
            f"SELECT mood, snippet, created_at FROM mood_log "
            f"WHERE created_at >= ? AND {clause} ORDER BY id DESC LIMIT 5000",
            (cutoff_utc, *user_args),
        ).fetchall()
    finally:
        conn.close()
    cutoff = datetime.now(TZ) - timedelta(days=days)
    in_range = [r for r in rows if _row_local(r) >= cutoff]
    if not in_range:
        return {"total": 0, "by_mood": {}, "negative_topics": [], "peak_hours": ""}

    by_mood = Counter(r["mood"] for r in in_range)
    topic_counter: Counter = Counter()
    for r in in_range:
        if r["mood"] in NEGATIVE_MOODS and r["snippet"]:
            topic_counter[r["snippet"]] += 1
    negative_topics = [s for s, _ in topic_counter.most_common(5)]

    hour_buckets = Counter(_row_local(r).hour // 3 * 3 for r in in_range)
    top_bucket = hour_buckets.most_common(1)[0][0]
    return {
        "total": len(in_range),
        "by_mood": dict(by_mood.most_common()),
        "negative_topics": negative_topics,
        "peak_hours": f"{top_bucket:02d}-{top_bucket + 2:02d} 点",
    }


def weekly_report_section(days: int = 7, user_id: str | None = None) -> str:
    """周报"本周情绪"节（零 LLM，确定性生成；无数据返回空串）。"""
    if user_id is None:
        # 兼容旧 monkeypatch 替身（只接受 days 一个参数）。
        s = get_weekly_stats(days)
    else:
        s = get_weekly_stats(days, user_id=user_id)
    if not s["total"]:
        return ""
    lines = [
        "## 本周情绪",
        f"- 情绪记录 {s['total']} 条："
        + "、".join(f"{k}×{v}" for k, v in s["by_mood"].items()),
    ]
    if s["peak_hours"]:
        lines.append(f"- 情绪高峰：{s['peak_hours']}")
    if s["negative_topics"]:
        lines.append("- 最常触发负面情绪的话题：")
        lines += [f"  {i}. {t}" for i, t in enumerate(s["negative_topics"], 1)]
    return "\n".join(lines)
