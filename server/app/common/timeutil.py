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


def utc_iso() -> str:
    """带微秒与时区标记的 UTC ISO 串（memories/facts 等模块的历史格式）。

    注意与 utc_str 的区别：utc_str 无微秒无时区标记（reminders 字符串比较
    依赖该格式），两者不可混用——存量数据按各自格式做字典序比较。
    """
    return datetime.now(timezone.utc).isoformat()


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
