"""时间工具统一收口：时区、now、行级时间展示、时段划分。

此前 9+ 个文件各自复制 ZoneInfo("Asia/Shanghai")/utc now/时段划分，
行为已有细微漂移（chat.py 与 mood.py 的时段边界、reminders 与
qq_push 的 utc 格式）——一律改用本模块。
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")


def now_local() -> datetime:
    """北京时间（用户本地时区）。"""
    return datetime.now(TZ)


def now_utc() -> datetime:
    """带时区的 UTC now（入库时间戳统一用它，不再 naive now）。"""
    return datetime.now(timezone.utc)


def utc_str(dt: datetime | None = None) -> str:
    """入库格式：UTC 无微秒无时区标记（reminders 字符串比较依赖此格式）。"""
    dt = dt or now_utc()
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def row_local(ts: str) -> str:
    """UTC 入库时间戳 → 北京时间展示串（YYYY-MM-DD HH:MM）。"""
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return (ts or "")[:16]


def day_period(hour: int) -> str:
    """小时 → 时段名（凌晨/早上/上午/中午/下午/晚上）。"""
    if hour < 5:
        return "凌晨"
    if hour < 9:
        return "早上"
    if hour < 12:
        return "上午"
    if hour < 13:
        return "中午"
    if hour < 18:
        return "下午"
    return "晚上"
