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
from datetime import datetime
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

def record_mood(mood_name: str, msg: str) -> int:
    """入账一条情绪记录（UTC 存储，东八区解读）。"""
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO mood_log (mood, snippet, created_at) VALUES (?, ?, ?)",
            (mood_name, msg.strip()[:80], _utc_now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _utc_now() -> str:
    return datetime.now(TZ).astimezone(ZoneInfo("UTC")).isoformat()


def _row_local(row) -> datetime:
    dt = datetime.fromisoformat(row["created_at"])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(TZ)


def get_today_injection() -> str:
    """今日（东八区）情绪曲线：如 "今日情绪：烦躁×2、疲惫×1（下午）"。无记录返回空串。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT mood, created_at FROM mood_log ORDER BY id DESC LIMIT 200"
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

def get_streak_injection() -> str:
    """最近情绪连击：连续 2+ 轮负面情绪 → 降级为倾听模式指引。否则空串。

    连击有时效：最近一条负面记录超过 STREAK_FRESH_HOURS 即视为过期——
    昨天的烦躁不该让今天一整天都在倾听模式里。
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT mood, created_at FROM mood_log ORDER BY id DESC LIMIT ?",
            (STREAK_LOOKBACK,),
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


def get_state_injection() -> str:
    """今日曲线 + 连击降级，合并成一条注入（无则空串）。"""
    parts = [p for p in (get_today_injection(), get_streak_injection()) if p]
    return "\n".join(parts)
