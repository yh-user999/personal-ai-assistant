"""定时提醒：中文时间命令解析 + 提醒 CRUD + 到期查询。

第 6.24 课。解析规则只覆盖常见句式（明早/今晚/N分钟后/明天X点…），
解析不了就返回 None，由 chat 路由给用户一个用法提示，不瞎猜时间。
"""
from __future__ import annotations

import re
import secrets
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


def due_reminders(stale_hours: float = 24.0, claim_token: str | None = None) -> list[dict]:
    """原子领取到期提醒，防多实例重复推送；返回 token 供 mark_notified 使用。"""
    conn = connect()
    try:
        now_dt = _now()
        now = _utc_str(now_dt)
        stale_cutoff = _utc_str(now_dt - timedelta(hours=stale_hours))
        # 回收过期 sending 租约，允许重试。
        conn.execute(
            "UPDATE reminders SET status='pending', sending_token='', sending_at=NULL, sending_lease_expires_at=NULL "
            "WHERE status='sending' AND sending_lease_expires_at < ?", (now,)
        )
        token = claim_token or secrets.token_urlsafe(24)
        lease = _utc_str(now_dt + timedelta(minutes=5))
        rows = conn.execute(
            "SELECT id, content, remind_at FROM reminders WHERE status='pending' AND remind_at <= ? ORDER BY remind_at ASC LIMIT 10",
            (now,),
        ).fetchall()
        result = []
        for r in rows:
            updated = conn.execute(
                "UPDATE reminders SET status='sending', sending_token=?, sending_at=?, sending_lease_expires_at=? WHERE id=? AND status='pending'",
                (token, now, lease, r["id"]),
            )
            if updated.rowcount:
                result.append({"id": r["id"], "content": r["content"], "stale": bool(r["remind_at"] and r["remind_at"] < stale_cutoff), "sending_token": token})
        conn.commit()
        return result
    finally:
        conn.close()


def mark_notified(ids: list[int], sending_token: str | None = None) -> None:
    """推送成功后消费；支持 token 校验并兼容旧调用方。"""
    if not ids:
        return
    conn = connect()
    try:
        placeholders = ",".join("?" * len(ids))
        params: list = [*ids]
        predicate = f"id IN ({placeholders}) AND status IN ('sending','pending')"
        if sending_token:
            predicate += " AND (sending_token=? OR status='pending')"
            params.append(sending_token)
        conn.execute(
            f"UPDATE reminders SET status='notified', sending_token='', sending_lease_expires_at=NULL WHERE {predicate}",
            params,
        )
        conn.commit()
    finally:
        conn.close()


def release_claim(ids: list[int], sending_token: str | None = None) -> None:
    """推送失败时释放 sending claim，下一轮可重试。"""
    if not ids:
        return
    conn = connect()
    try:
        placeholders = ",".join("?" * len(ids))
        params: list = [*ids]
        predicate = f"id IN ({placeholders}) AND status='sending'"
        if sending_token:
            predicate += " AND sending_token=?"
            params.append(sending_token)
        conn.execute(
            f"UPDATE reminders SET status='pending', sending_token='', sending_at=NULL, sending_lease_expires_at=NULL WHERE {predicate}",
            params,
        )
        conn.commit()
    finally:
        conn.close()
