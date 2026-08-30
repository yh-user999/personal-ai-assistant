"""定时提醒：中文时间命令解析 + 提醒 CRUD + 到期查询。

第 6.24 课。解析规则只覆盖常见句式（明早/今晚/N分钟后/明天X点…），
解析不了就返回 None，由 chat 路由给用户一个用法提示，不瞎猜时间。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.common.timeutil import TZ, now_local, utc_str
from app.models.database import connect

TZ = ZoneInfo("Asia/Shanghai")


def _now() -> datetime:
    return now_local()


def _utc_str(dt: datetime) -> str:
    return utc_str(dt)


def _parse_abs_hour(text: str, base_date: datetime.date) -> datetime | None:
    """从文本解析绝对小时：支持 9点/9:30/下午3点/晚上10点/明早9点 等。"""
    m = re.search(
        r"(早上|上午|中午|下午|傍晚|晚上|今晚|明早|明晚|凌晨|夜里)?"
        r"(\d{1,2})[:：点时](\d{1,2})?分?",
        text,
    )
    if not m:
        return None
    prefix, hour_s, minute_s = m.group(1), m.group(2), m.group(3)
    hour = int(hour_s)
    minute = int(minute_s) if minute_s else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    if prefix in ("下午", "傍晚", "晚上", "今晚", "夜里", "明晚"):
        if hour < 12:
            hour += 12
    elif prefix == "凌晨" and hour == 12:
        hour = 0
    return datetime.combine(base_date, datetime.min.time()).replace(
        hour=hour, minute=minute, tzinfo=TZ
    )


def parse_reminder_cmd(msg: str) -> tuple[str, datetime] | None:
    """解析提醒命令，成功返回 (内容, 提醒时间 Asia/Shanghai)，失败返回 None。

    支持句式：
      - "30分钟后提醒我喝水" / "2小时后提醒我交周报"
      - "明早9点提醒我开会" / "明天15点提醒我取快递" / "明晚10点提醒我睡觉"
      - "今晚8点提醒我看球" / "下午3点提醒我打电话" / "9点提醒我下班"
    """
    now = _now()

    # ① 相对时间：N分钟后 / N小时后
    m = re.search(r"(\d{1,3})\s*分钟后提醒我[：:\s]*(.+)", msg)
    if m:
        return m.group(2).strip(), now + timedelta(minutes=int(m.group(1)))
    m = re.search(r"(\d{1,3})\s*小时后提醒我[：:\s]*(.+)", msg)
    if m:
        return m.group(2).strip(), now + timedelta(hours=int(m.group(1)))

    # ② 明天系
    m = re.search(
        r"(明早|明天早上|明上午|明晚|明下午|明天)[：:\s]*(.*?)[，,]?提醒我[：:\s]*(.+)",
        msg,
    )
    if m:
        prefix, time_part, content = m.group(1), m.group(2).strip(), m.group(3).strip()
        base = now.date() + timedelta(days=1)
        if time_part:
            # 把"明晚10点"整体交给时段解析（明晚=晚上→22点），单传"10点"会算成上午
            dt = _parse_abs_hour(prefix + time_part, base)
            if dt:
                return content, dt
        return content, datetime.combine(base, datetime.min.time()).replace(
            hour=9, tzinfo=TZ
        )

    # ③ 今天/今晚/下午…系
    m = re.search(
        r"(今天|今晚|晚上|下午|傍晚|早上|上午|中午)?[：:\s]*"
        r"(\d{1,2}[:：点时]\d{0,2}分?)[，,]?提醒我[：:\s]*(.+)",
        msg,
    )
    if m:
        prefix, time_part, content = m.group(1) or "", m.group(2), m.group(3).strip()
        dt = _parse_abs_hour(prefix + time_part, now.date())
        if dt:
            if dt < now:
                dt += timedelta(days=1)
            return content, dt
    return None


def add_reminder(content: str, remind_at: datetime) -> int:
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO reminders (content, remind_at, status, created_at) "
            "VALUES (?, ?, 'pending', ?)",
            (content, _utc_str(remind_at), _utc_str(_now())),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _db_to_local(iso: str) -> str:
    """库里的 UTC 字符串 → 北京时间展示。fromisoformat 对无时区标记的字符串
    会按 naive 处理，必须先显式挂 UTC，否则 astimezone 会当成服务器本地时间。"""
    dt = datetime.fromisoformat(iso).replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(TZ).strftime("%m月%d日 %H:%M")


def list_pending() -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, content, remind_at FROM reminders WHERE status='pending' "
            "ORDER BY remind_at ASC LIMIT 20"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": r["id"],
            "content": r["content"],
            "remind_at": _db_to_local(r["remind_at"]),
        }
        for r in rows
    ]


def cancel_by_keyword(keyword: str) -> int:
    """按内容片段取消，返回取消条数。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, content FROM reminders WHERE status='pending'"
        ).fetchall()
        hit = [r for r in rows if keyword.strip() in r["content"]]
        for r in hit:
            conn.execute("UPDATE reminders SET status='cancelled' WHERE id=?", (r["id"],))
        conn.commit()
        return len(hit)
    finally:
        conn.close()


def due_reminders(stale_hours: float = 24.0) -> list[dict]:
    """取出已到期的未提醒项（不消费，调用方推送成功后 mark_notified）。

    消费语义与推送解耦：QQ 是提醒的唯一通道，若"选中即标记"，NapCat
    掉线期间的到期提醒会被静默吞掉（标记了但没推出去，永不重试）。
    改为推送确认后消费，失败项下一轮重推。

    stale_hours：超龄分组阈值。掉线超阈值的老项单独标 stale=True——
    调用方可合并成摘要推送，避免 NapCat 恢复后积压轰炸。
    """
    conn = connect()
    try:
        now = _utc_str(_now())
        stale_cutoff = _utc_str(_now() - timedelta(hours=stale_hours))
        rows = conn.execute(
            "SELECT id, content, remind_at FROM reminders WHERE status='pending' AND remind_at <= ? "
            "ORDER BY remind_at ASC LIMIT 10",
            (now,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "content": r["content"],
                "stale": bool(r["remind_at"] and r["remind_at"] < stale_cutoff),
            }
            for r in rows
        ]
    finally:
        conn.close()


def mark_notified(ids: list[int]) -> None:
    """推送成功后消费（幂等：重复标记无副作用）。"""
    if not ids:
        return
    conn = connect()
    try:
        conn.execute(
            "UPDATE reminders SET status='notified' WHERE id IN ({})".format(
                ",".join("?" * len(ids))
            ),
            ids,
        )
        conn.commit()
    finally:
        conn.close()
