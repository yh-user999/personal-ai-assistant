"""群成员画像：为可持续对话记住"聊过的人"，与私聊画像物理隔离。

设计约束（不是建议，是实现边界）：
- **独立表 group_member_profile**：不复用私聊 ``profile`` 表。私聊注入按
  ``user_id`` 查 ``profile``，若混表，群成员数据会串进主人私聊上下文。
- **按 (群, 成员) 分别归档**：同一人在不同群互不共享，避免跨群关联画像。
- **只记对话所需维度**：称呼、话题偏好、表达风格。明确不推断政治立场、
  宗教、健康、性取向、收入等敏感属性——这些对聊天连续性没有帮助，
  一旦出错或泄露的伤害却不可撤回。
- **有保留期**：超过 ``RETENTION_DAYS`` 未活跃的记录可被清理，不无限积累。
- **群内不透露画像存在**，也不因画像差异给不同成员不同能力。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from app.models.database import connect

logger = logging.getLogger("assistant.group_profile")

# 仅这三个维度：都是"怎么跟这个人接着聊"直接需要的信息。
ALLOWED_DIMENSIONS = ("preferred_name", "topics", "style")

# 敏感维度黑名单：即使模型返回也丢弃，防止提示注入或模型自作主张。
BLOCKED_DIMENSIONS = frozenset({
    "politics", "religion", "health", "sexual_orientation", "income",
    "address", "phone", "id_number", "family", "race", "ethnicity",
})

MAX_VALUE_CHARS = 200
RETENTION_DAYS = 90


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_table() -> None:
    """建表（幂等）。独立于私聊 profile，避免任何混读。"""
    conn = connect()
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS group_member_profile (
                 group_id TEXT NOT NULL,
                 member_id TEXT NOT NULL,
                 dimension TEXT NOT NULL,
                 value TEXT NOT NULL,
                 confidence REAL DEFAULT 0.5,
                 updated_at TEXT NOT NULL,
                 PRIMARY KEY(group_id, member_id, dimension)
               )"""
        )
        conn.commit()
    finally:
        conn.close()


def remember(group_id: str, member_id: str, dimension: str, value: str, confidence: float = 0.6) -> bool:
    """写入/更新一条群成员画像；维度不在白名单则丢弃。"""
    group = str(group_id or "").strip()
    member = str(member_id or "").strip()
    dim = str(dimension or "").strip()
    text = " ".join(str(value or "").split())[:MAX_VALUE_CHARS]
    if not (group and member and text):
        return False
    if dim in BLOCKED_DIMENSIONS or dim not in ALLOWED_DIMENSIONS:
        logger.debug("群画像维度被拒: %s", dim)
        return False
    ensure_table()
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO group_member_profile
                 (group_id, member_id, dimension, value, confidence, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(group_id, member_id, dimension) DO UPDATE
                 SET value=excluded.value, confidence=excluded.confidence,
                     updated_at=excluded.updated_at""",
            (group, member, dim, text, max(0.0, min(1.0, float(confidence))), _now()),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def get_injection(group_id: str, member_id: str) -> str:
    """取该群该成员的画像注入文本；无数据返回空串。

    只读本群本人，绝不跨群、绝不读取私聊 profile。
    """
    group = str(group_id or "").strip()
    member = str(member_id or "").strip()
    if not (group and member):
        return ""
    ensure_table()
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT dimension, value FROM group_member_profile
               WHERE group_id=? AND member_id=? AND confidence >= 0.4
               ORDER BY dimension""",
            (group, member),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return ""
    lines = "\n".join(f"- {r['dimension']}: {r['value']}" for r in rows)
    return (
        "【这位群友的已知情况】（仅用于把话接得自然，"
        "不要主动复述、不要说明你有记录）\n" + lines + "\n\n"
    )


def purge_expired(now: datetime | None = None) -> int:
    """清理超过保留期未更新的记录，返回删除条数。"""
    ensure_table()
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=RETENTION_DAYS)).isoformat()
    conn = connect()
    try:
        cur = conn.execute("DELETE FROM group_member_profile WHERE updated_at < ?", (cutoff,))
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def forget_member(group_id: str, member_id: str) -> int:
    """删除某群某成员的全部画像（应对"别记我"的要求）。"""
    ensure_table()
    conn = connect()
    try:
        cur = conn.execute(
            "DELETE FROM group_member_profile WHERE group_id=? AND member_id=?",
            (str(group_id or "").strip(), str(member_id or "").strip()),
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def forget_group(group_id: str) -> int:
    """删除整个群的画像数据。"""
    ensure_table()
    conn = connect()
    try:
        cur = conn.execute(
            "DELETE FROM group_member_profile WHERE group_id=?", (str(group_id or "").strip(),)
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def stats() -> dict:
    """诊断用统计，不含画像内容。"""
    ensure_table()
    conn = connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM group_member_profile").fetchone()[0]
        members = conn.execute(
            "SELECT COUNT(DISTINCT group_id || ':' || member_id) FROM group_member_profile"
        ).fetchone()[0]
        groups = conn.execute("SELECT COUNT(DISTINCT group_id) FROM group_member_profile").fetchone()[0]
    finally:
        conn.close()
    return {"rows": total, "members": members, "groups": groups}


def parse_updates(payload: object) -> list[tuple[str, str, float]]:
    """解析模型返回的画像更新，过滤非法与敏感维度。

    单独成函数便于测试：模型输出不可信，必须在入库前收敛。
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return []
    if not isinstance(payload, dict):
        return []
    out: list[tuple[str, str, float]] = []
    for item in payload.get("updates", []) or []:
        if not isinstance(item, dict):
            continue
        dim = str(item.get("dimension", "")).strip()
        if dim not in ALLOWED_DIMENSIONS or dim in BLOCKED_DIMENSIONS:
            continue
        value = " ".join(str(item.get("value", "")).split())[:MAX_VALUE_CHARS]
        if not value:
            continue
        try:
            conf = max(0.0, min(1.0, float(item.get("confidence", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        out.append((dim, value, conf))
    return out
