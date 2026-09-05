"""主观时间：把机械日期换成人记事的方式。

人不说"2026-08-28 那天"，人说"你写小说设定那阵子"、"周报出来之后"。
记忆注入现在是 `[记忆] 2026-08-20: 内容`——用户看到的是日志，不是回忆。
这里给每条记忆配一个事件锚点，让她能说"就在你调完向量维度那会儿"。

锚点来源（都是已有数据，零 LLM）：
- daily_summaries 的首行标题（每晚 22:00 已生成，是当天的主线概括）
- work_log 的内容（用户手动记的，本身就是他认为值得记的事）

注意不用 importance 高的记忆做锚点：importance 只按召回次数累加，
实测最高的几条是"你好""再确认一下"这类短句（越短越容易被检索命中），
拿它们当锚点会得到"在你说『你好』那阵子"这种废话。
"""
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.models.database import connect

TZ = ZoneInfo("Asia/Shanghai")

# 锚点标题最长字数（超出截断，注入里不该出现长句）
ANCHOR_MAX_CHARS = 18
# 同期判定：与锚点相差几天内算"同期"
SAME_PERIOD_DAYS = 1
# 最多回看多少天的锚点（太老的锚点用户自己也想不起来）
ANCHOR_LOOKBACK_DAYS = 30


# 锚点里要剥掉的时间前缀（work_log 常以"下午3点到5点"开头，
# 留着会变成"下午3点到5点完成了X那阵子"这种别扭说法）
_TIME_PREFIX = re.compile(
    r"^(上午|中午|下午|傍晚|晚上|凌晨|早上|今天|昨天)?\s*"
    r"\d{1,2}\s*[:：点]?\s*\d{0,2}\s*"
    r"(分)?\s*(到|-|~|至)\s*\d{1,2}\s*[:：点]?\s*\d{0,2}\s*(分)?\s*"
)
# 小结正文常见的套话开头，剥掉才剩下真正的事
_FILLER_PREFIX = re.compile(r"^(今日|今天|本日)(主要)?(围绕|是|在)?\s*")


def _clean_title(text: str) -> str:
    """从小结/日志正文里提炼可当锚点的短语。

    只在"完整语义单元"处切断——宁可返回空串放弃这个锚点，也不要生成
    "今日主要围绕接码平台记录、玄幻架空明那阵子"这种截在半个词上的说法。
    """
    lines = (text or "").strip().splitlines()
    if not lines:  # 空串或纯空白：strip 后 splitlines() 返回 []，直接下标会 IndexError
        return ""
    first = lines[0].strip()
    for mark in ("**", "##", "#", "- ", "* ", "💡", "💭"):
        first = first.replace(mark, "")
    first = _TIME_PREFIX.sub("", first.strip())
    first = _FILLER_PREFIX.sub("", first)
    first = first.strip(" 　:：。，,、")
    if not first:
        return ""
    if len(first) <= ANCHOR_MAX_CHARS:
        return first
    # 超长：只在句读处切，切不出合适长度就放弃（不硬截）
    for sep in ("：", ":", "，", ",", "、", "。", " "):
        idx = first.find(sep)
        if 4 <= idx <= ANCHOR_MAX_CHARS:
            return first[:idx]
    return ""


def get_anchors(
    lookback_days: int = ANCHOR_LOOKBACK_DAYS,
    user_id: str | None = None,
) -> dict[str, str]:
    """{日期(YYYY-MM-DD): 锚点标题}，仅读取当前主体的日报和工作日志。"""
    from app.core.memory import _user_scope, normalize_user_id

    cutoff = (datetime.now(TZ).date() - timedelta(days=lookback_days)).isoformat()
    uid = normalize_user_id(user_id)
    clause, args = _user_scope(uid)
    anchors: dict[str, str] = {}
    conn = connect()
    try:
        for row in conn.execute(
            f"SELECT date, content FROM daily_summaries WHERE date >= ? AND {clause} ORDER BY date",
            (cutoff, *args),
        ).fetchall():
            title = _clean_title(row["content"])
            if title:
                anchors[row["date"]] = title
        # work_log 覆盖同日的小结标题：用户手动记录的事更贴近他自己的记忆
        for row in conn.execute(
            f"SELECT date, content FROM work_log WHERE date >= ? AND {clause} ORDER BY id",
            (cutoff, *args),
        ).fetchall():
            title = _clean_title(row["content"])
            if title:
                anchors[row["date"]] = title
    finally:
        conn.close()
    return anchors


def _parse_day(ts: str) -> date | None:
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ).date()


def describe(ts: str, anchors: dict[str, str] | None = None,
             today: date | None = None) -> str:
    """一条记忆的时间描述：优先相对日（今天/昨天），否则挂最近的事件锚点。

    返回空串表示没有可用描述（调用方退回原始日期）。
    """
    day = _parse_day(ts)
    if day is None:
        return ""
    today = today or datetime.now(TZ).date()
    delta = (today - day).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "昨天"
    if delta == 2:
        return "前天"

    anchors = anchors if anchors is not None else get_anchors()
    if not anchors:
        return ""
    # 同期：锚点日期与记忆日期相差 ≤ SAME_PERIOD_DAYS
    best: tuple[int, str] | None = None
    for a_date, title in anchors.items():
        a_day = _parse_day(a_date + "T00:00:00")
        if a_day is None:
            continue
        gap = abs((a_day - day).days)
        if gap <= SAME_PERIOD_DAYS and (best is None or gap < best[0]):
            best = (gap, title)
    if best:
        return f"{title_phrase(best[1])}那阵子"
    return ""


def title_phrase(title: str) -> str:
    """锚点标题转成可嵌进句子的说法（去掉结尾的"的一天"这类后缀）。"""
    for suffix in ("的一天", "的一天。", "日", "。"):
        if title.endswith(suffix) and len(title) > len(suffix) + 1:
            title = title[: -len(suffix)]
    return title.strip()


def format_injection(memories: list[dict], user_id: str | None = None) -> str:
    """记忆注入的主观时间版本，锚点仅来自当前主体。"""
    if not memories:
        return ""
    anchors = get_anchors(user_id=user_id)
    today = datetime.now(TZ).date()
    parts = []
    for m in memories:
        ts = m.get("ts") or ""
        content = m.get("content", "")
        if m.get("summary"):
            content = f"{m['summary']}（{content[:50]}）"
        when = describe(ts, anchors, today) or ts[:10]
        parts.append(f"[记忆] {when}: {content[:120]}")
    return "\n".join(parts)
