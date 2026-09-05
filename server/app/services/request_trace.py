"""请求决策轨迹（检索可观测性 P0）：每轮对话的检索决策链落一行，可回放可统计。

字段见 models/database.py 的 request_traces 表。写入是 fire-and-forget：
失败只记日志，绝不影响聊天回复。清理由 evict_stale 顺带执行（30 天保留）。
"""
import json
import logging
import sqlite3
from datetime import datetime, timezone

from app.models.database import connect

logger = logging.getLogger("assistant.trace")

TRACE_RETENTION_DAYS = 30


def record(
    user_id: str,
    query: str,
    routing: dict,
    retrieval_path: str,
    vector_degraded: bool,
    healer_words: list[str],
    injection_bytes: dict,
    search_ms: int,
) -> bool:
    """落一行决策轨迹（同步 SQL，毫秒级）。失败返回 False 并记日志。"""
    try:
        from app.core.memory import normalize_user_id

        uid = normalize_user_id(user_id)
        conn = connect()
        try:
            conn.execute(
                """INSERT INTO request_traces
                   (user_id, query, ts, routing, retrieval_path, vector_degraded,
                    healer, injection_bytes, search_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    uid,
                    (query or "")[:500],
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(routing or {}, ensure_ascii=False),
                    retrieval_path or "",
                    1 if vector_degraded else 0,
                    json.dumps(healer_words or [], ensure_ascii=False),
                    json.dumps(injection_bytes or {}, ensure_ascii=False),
                    int(search_ms or 0),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except (sqlite3.Error, TypeError, ValueError, RuntimeError) as e:
        logger.warning("[trace] 决策轨迹写入失败（不影响回复）: %s", e)
        return False


def cleanup_stale(days: int = TRACE_RETENTION_DAYS) -> int:
    """删除超过保留期的轨迹行。返回删除行数。"""
    from datetime import timedelta

    # 同为 UTC ISO 格式，字典序 = 时间序
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = connect()
    try:
        n = conn.execute(
            "DELETE FROM request_traces WHERE ts < ?", (cutoff,)
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    if n:
        logger.info("[trace] 清理过期决策轨迹 %d 行", n)
    return n
