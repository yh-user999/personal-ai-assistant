"""群聊事件驱动的轻量关怀。

只在当前群消息触发时给出一句关心或一次跟进，不做定时群发；危机和敏感
内容不进入主动关怀路径。台账只保存群/成员作用域、抽象信号类型和时间，
不保存原始消息正文。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.core.memory import normalize_user_id
from app.models.database import connect

MAX_GROUP_ID_CHARS = 64
FAMILIARITY_THRESHOLD = 0.2
MIN_AFFINITY = -0.15
DEFAULT_COOLDOWN_SECONDS = 21600.0
DEFAULT_DAILY_LIMIT = 2
DEFAULT_MAX_FOLLOWUPS = 1

_EMOTION_RE = re.compile(r"焦虑|难受|崩溃|烦|累|沮丧|生气|压力|睡不着|委屈|撑不住|心累|低落")
_SENSITIVE_RE = re.compile(
    r"身份证|手机号|电话号码|住址|家庭住址|密码|口令|token|密钥|银行卡|收入|工资|性取向|宗教|政治立场",
    re.IGNORECASE,
)
_CRISIS_RE = re.compile(r"自杀|自残|不想活|结束生命|伤害自己|活不下去")


@dataclass(frozen=True)
class CareDecision:
    eligible: bool
    kind: str = ""
    reason: str = ""
    signals: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible": bool(self.eligible),
            "kind": self.kind[:24],
            "reason": self.reason[:48],
            "signals": list(self.signals)[:6],
        }


def _group(group_id: str) -> str:
    value = str(group_id or "").strip()
    if not value or len(value) > MAX_GROUP_ID_CHARS:
        raise ValueError("group_id is required")
    return value


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)


def _parse(value: object) -> datetime | None:
    try:
        current = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)


def _relationship_ok(relationship: dict[str, Any]) -> tuple[bool, str]:
    try:
        familiarity = max(0.0, min(1.0, float(relationship.get("familiarity", 0.0))))
        affinity = max(-1.0, min(1.0, float(relationship.get("affinity", 0.0))))
    except (AttributeError, TypeError, ValueError):
        return False, "relationship_unavailable"
    if familiarity < FAMILIARITY_THRESHOLD:
        return False, "member_not_familiar"
    if affinity < MIN_AFFINITY:
        return False, "relationship_too_cold"
    return True, "familiar_group_member"


def _latest_and_daily(group: str, uid: str, current: datetime) -> tuple[dict[str, Any] | None, int]:
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    conn = connect()
    try:
        latest = conn.execute(
            "SELECT kind, sent_at, followup_count FROM group_care_events "
            "WHERE group_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
            (group, uid),
        ).fetchone()
        daily = conn.execute(
            "SELECT COUNT(*) AS count FROM group_care_events "
            "WHERE group_id=? AND user_id=? AND sent_at>=?",
            (group, uid, day_start),
        ).fetchone()["count"]
        return (dict(latest) if latest else None), int(daily or 0)
    finally:
        conn.close()


def assess(
    group_id: str,
    user_id: str,
    message: str,
    relationship: dict[str, Any] | None = None,
    scene: dict[str, Any] | None = None,
    *,
    enabled: bool = True,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    daily_limit: int = DEFAULT_DAILY_LIMIT,
    max_followups: int = DEFAULT_MAX_FOLLOWUPS,
    now: datetime | None = None,
) -> CareDecision:
    """判断当前消息是否允许一次群内轻量关怀。"""
    if not enabled:
        return CareDecision(False, reason="disabled")
    try:
        group = _group(group_id)
        uid = normalize_user_id(user_id)
    except ValueError:
        return CareDecision(False, reason="scope_missing")
    text = " ".join(str(message or "").split())[:800]
    if not text:
        return CareDecision(False, reason="empty_message")
    if _CRISIS_RE.search(text):
        return CareDecision(False, reason="crisis_signal")
    if _SENSITIVE_RE.search(text):
        return CareDecision(False, reason="sensitive_subject")
    if not _EMOTION_RE.search(text):
        return CareDecision(False, reason="no_emotion_signal")

    relationship_ok, relationship_reason = _relationship_ok(relationship or {})
    if not relationship_ok:
        return CareDecision(False, reason=relationship_reason)
    scene_data = scene or {}
    if str(scene_data.get("atmosphere") or "").strip().lower() == "tense":
        return CareDecision(False, reason="tense_scene")
    if bool(scene_data.get("has_recent_bot_reply")):
        return CareDecision(False, reason="recent_bot_reply")

    current = _now(now)
    try:
        latest, daily_count = _latest_and_daily(group, uid, current)
    except (OSError, sqlite3.Error):
        return CareDecision(False, reason="state_unavailable")
    if daily_count >= max(0, int(daily_limit)):
        return CareDecision(False, reason="daily_limit")

    kind = "initial"
    if latest:
        sent_at = _parse(latest.get("sent_at"))
        if sent_at and (current - sent_at).total_seconds() < max(0.0, float(cooldown_seconds)):
            return CareDecision(False, reason="cooldown")
        if int(latest.get("followup_count") or 0) >= max(0, int(max_followups)):
            return CareDecision(False, reason="followup_limit")
        kind = "followup"
    return CareDecision(
        True,
        kind=kind,
        reason=relationship_reason,
        signals=("emotion_signal", "care_followup" if kind == "followup" else "care_signal"),
    )


def record_sent(group_id: str, user_id: str, kind: str = "initial", *, now: datetime | None = None) -> int:
    """成功且非空回复后记录一次关怀；不保存消息正文。"""
    group = _group(group_id)
    uid = normalize_user_id(user_id)
    current = _now(now).isoformat()
    conn = connect()
    try:
        latest = conn.execute(
            "SELECT followup_count FROM group_care_events "
            "WHERE group_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
            (group, uid),
        ).fetchone()
        previous = int(latest["followup_count"] or 0) if latest else 0
        followup_count = previous + 1 if kind == "followup" else previous
        cur = conn.execute(
            "INSERT INTO group_care_events "
            "(group_id,user_id,kind,sent_at,followup_count) VALUES (?,?,?,?,?)",
            (group, uid, kind[:24], current, followup_count),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def get_snapshot(group_id: str, user_id: str) -> dict[str, Any]:
    group = _group(group_id)
    uid = normalize_user_id(user_id)
    conn = connect()
    try:
        row = conn.execute(
            "SELECT kind, sent_at, followup_count FROM group_care_events "
            "WHERE group_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
            (group, uid),
        ).fetchone()
        if not row:
            return {}
        return {
            "kind": str(row["kind"] or "")[:24],
            "sent_at": str(row["sent_at"] or ""),
            "followup_count": max(0, int(row["followup_count"] or 0)),
        }
    finally:
        conn.close()


def clear(group_id: str | None = None, user_id: str | None = None) -> int:
    clauses = []
    args: list[str] = []
    if group_id:
        clauses.append("group_id=?")
        args.append(_group(group_id))
    if user_id:
        clauses.append("user_id=?")
        args.append(normalize_user_id(user_id))
    if not clauses:
        return 0
    conn = connect()
    try:
        cur = conn.execute("DELETE FROM group_care_events WHERE " + " AND ".join(clauses), args)
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()
