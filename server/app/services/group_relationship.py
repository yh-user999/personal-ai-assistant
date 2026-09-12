"""群内关系状态：按 group_id + user_id 隔离的轻量互动统计。

只保存熟悉度、亲近度和玩笑容忍度等抽象数值，不保存消息正文，不推断敏感
属性。它只影响表达方式，不能改变权限、事实判断或工具调用。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.core.memory import normalize_user_id
from app.models.database import connect

MAX_GROUP_ID_CHARS = 64
_POSITIVE_RE = re.compile(r"哈哈+|笑死|好耶|太爽|绝了|谢谢|辛苦|有用|解决了|搞定")
_NEGATIVE_RE = re.compile(r"滚|闭嘴|垃圾|骗子|傻|蠢|烦死|别说了")
_BANTER_RE = re.compile(r"哈哈+|笑死|离谱|好家伙|不会吧|你又|又来了")


def _group(group_id: str) -> str:
    value = str(group_id or "").strip()
    if not value or len(value) > MAX_GROUP_ID_CHARS:
        raise ValueError("group_id is required")
    return value


def _now(now: str | None = None) -> str:
    return now or datetime.now(timezone.utc).isoformat()


def _load(conn, group_id: str, user_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM group_relationships WHERE group_id=? AND user_id=?",
        (group_id, user_id),
    ).fetchone()
    if row:
        return dict(row)
    timestamp = _now()
    conn.execute(
        "INSERT INTO group_relationships "
        "(group_id,user_id,last_seen_at,updated_at) VALUES (?,?,?,?)",
        (group_id, user_id, timestamp, timestamp),
    )
    return {
        "group_id": group_id,
        "user_id": user_id,
        "familiarity": 0.0,
        "affinity": 0.0,
        "banter_tolerance": 0.2,
        "interaction_count": 0,
        "positive_count": 0,
        "negative_count": 0,
        "last_seen_at": timestamp,
        "updated_at": timestamp,
    }


def _snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "familiarity": round(max(0.0, min(1.0, float(row.get("familiarity", 0.0)))), 3),
        "affinity": round(max(-1.0, min(1.0, float(row.get("affinity", 0.0)))), 3),
        "banter_tolerance": round(max(0.0, min(1.0, float(row.get("banter_tolerance", 0.2)))), 3),
        "interaction_count": max(0, int(row.get("interaction_count", 0))),
        "positive_count": max(0, int(row.get("positive_count", 0))),
        "negative_count": max(0, int(row.get("negative_count", 0))),
    }


def observe_message(
    group_id: str,
    user_id: str,
    message: str,
    *,
    directed: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    """记录一次群互动，返回不含身份的关系快照。"""
    group = _group(group_id)
    uid = normalize_user_id(user_id)
    text = " ".join(str(message or "").split())[:500]
    timestamp = _now(now)
    positive = bool(_POSITIVE_RE.search(text))
    negative = bool(_NEGATIVE_RE.search(text))
    banter = bool(_BANTER_RE.search(text))
    familiarity_delta = 0.04 if directed else 0.015
    affinity_delta = 0.03 if positive else -0.04 if negative else 0.0
    tolerance_delta = 0.04 if banter and not negative else -0.08 if negative else 0.0

    conn = connect()
    try:
        row = _load(conn, group, uid)
        familiarity = min(1.0, float(row["familiarity"]) + familiarity_delta)
        affinity = max(-1.0, min(1.0, float(row["affinity"]) + affinity_delta))
        tolerance = max(0.0, min(1.0, float(row["banter_tolerance"]) + tolerance_delta))
        conn.execute(
            """UPDATE group_relationships
               SET familiarity=?, affinity=?, banter_tolerance=?
                   ,interaction_count=interaction_count+1
                   ,positive_count=positive_count+?
                   ,negative_count=negative_count+?
                   ,last_seen_at=?, updated_at=?
               WHERE group_id=? AND user_id=?""",
            (
                familiarity, affinity, tolerance, int(positive), int(negative),
                timestamp, timestamp, group, uid,
            ),
        )
        conn.commit()
        row.update(
            familiarity=familiarity,
            affinity=affinity,
            banter_tolerance=tolerance,
            interaction_count=int(row["interaction_count"]) + 1,
            positive_count=int(row["positive_count"]) + int(positive),
            negative_count=int(row["negative_count"]) + int(negative),
        )
        return _snapshot(row)
    finally:
        conn.close()


def record_reply(
    group_id: str,
    user_id: str,
    *,
    action: str = "answer",
    success: bool = True,
    now: str | None = None,
) -> dict[str, Any]:
    """记录机器人对该群友的成功回应；失败或空回复不增加亲近度。"""
    group = _group(group_id)
    uid = normalize_user_id(user_id)
    timestamp = _now(now)
    if not success:
        return get_snapshot(group, uid)
    conn = connect()
    try:
        row = _load(conn, group, uid)
        familiarity = min(1.0, float(row["familiarity"]) + 0.03)
        affinity_delta = 0.02 if action in {"answer", "banter", "tease", "ask_back", "interject"} else 0.0
        tolerance_delta = 0.03 if action in {"banter", "tease"} else 0.0
        affinity = max(-1.0, min(1.0, float(row["affinity"]) + affinity_delta))
        tolerance = max(0.0, min(1.0, float(row["banter_tolerance"]) + tolerance_delta))
        conn.execute(
            """UPDATE group_relationships
               SET familiarity=?, affinity=?, banter_tolerance=?, last_seen_at=?, updated_at=?
               WHERE group_id=? AND user_id=?""",
            (familiarity, affinity, tolerance, timestamp, timestamp, group, uid),
        )
        conn.commit()
        row.update(familiarity=familiarity, affinity=affinity, banter_tolerance=tolerance)
        return _snapshot(row)
    finally:
        conn.close()


def get_snapshot(group_id: str, user_id: str) -> dict[str, Any]:
    group = _group(group_id)
    uid = normalize_user_id(user_id)
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM group_relationships WHERE group_id=? AND user_id=?",
            (group, uid),
        ).fetchone()
        return _snapshot(dict(row)) if row else {}
    finally:
        conn.close()


def get_injection(group_id: str, user_id: str) -> str:
    """输出有限自然语言等级，不泄露数值和主体标识。"""
    snapshot = get_snapshot(group_id, user_id)
    if not snapshot:
        return "群内关系：刚认识，先保持自然和礼貌。"
    familiarity = snapshot["familiarity"]
    affinity = snapshot["affinity"]
    tolerance = snapshot["banter_tolerance"]
    familiar_label = "刚认识" if familiarity < 0.2 else "逐渐熟悉" if familiarity < 0.55 else "比较熟悉"
    if tolerance < 0.25:
        banter_label = "先不要主动开过火的玩笑"
    elif tolerance < 0.6:
        banter_label = "可以适度接梗"
    else:
        banter_label = "可以轻松接梗，但仍只针对当前话题"
    affinity_label = "互动偏冷" if affinity < -0.25 else "互动较亲近" if affinity > 0.25 else "互动中性"
    return f"群内关系：{familiar_label}、{affinity_label}；{banter_label}。"


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
        cur = conn.execute("DELETE FROM group_relationships WHERE " + " AND ".join(clauses), args)
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()
