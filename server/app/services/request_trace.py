"""请求决策轨迹（检索可观测性 P0）：每轮对话的检索决策链落一行，可回放可统计。

字段见 models/database.py 的 request_traces 表。写入是 fire-and-forget：
失败只记日志，绝不影响聊天回复。清理由 evict_stale 顺带执行（30 天保留）。
"""
import json
import logging
import sqlite3
from datetime import datetime, timezone

from app.models.database import connect
from app.services.sanitize import sanitize

logger = logging.getLogger("assistant.trace")

TRACE_RETENTION_DAYS = 30


def _safe_text(value: object, limit: int = 160) -> str:
    return sanitize(str(value or ""))[:limit]


def _summary_map(value: object, *, allowed: set[str], numeric: set[str] = set()) -> dict:
    if not isinstance(value, dict):
        return {}
    result: dict = {}
    for key in allowed:
        if key not in value:
            continue
        raw = value[key]
        if key in numeric:
            try:
                result[key] = max(0, int(raw))
            except (TypeError, ValueError):
                continue
        elif isinstance(raw, bool):
            result[key] = raw
        elif isinstance(raw, (int, float)):
            result[key] = raw
        elif isinstance(raw, (list, tuple)):
            result[key] = [_safe_text(item, 80) for item in list(raw)[:20]]
        else:
            result[key] = _safe_text(raw)
    return result


def _safe_routing(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in ("domains", "docs"):
        raw = value.get(key)
        if isinstance(raw, (list, tuple)):
            result[key] = [_safe_text(item, 100) for item in list(raw)[:20]]
    return result


def _safe_stages(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {}
    for name, raw in list(value.items())[:20]:
        if not isinstance(raw, dict):
            continue
        item = {}
        status = raw.get("status")
        if status:
            item["status"] = _safe_text(status, 24)
        if "elapsed_ms" in raw:
            try:
                item["elapsed_ms"] = max(0, int(raw["elapsed_ms"]))
            except (TypeError, ValueError):
                pass
        if raw.get("error"):
            item["error"] = _safe_text(raw["error"], 80)
        result[_safe_text(name, 48)] = item
    return result


def record(
    user_id: str,
    query: str,
    routing: dict,
    retrieval_path: str,
    vector_degraded: bool,
    healer_words: list[str],
    injection_bytes: dict,
    search_ms: int,
    *,
    trace_id: str = "",
    request_id: str = "",
    channel: str = "chat",
    route_name: str = "/api/chat",
    retrieval: dict | None = None,
    stages: dict | None = None,
    reflection: dict | None = None,
    total_latency_ms: int = 0,
    status: str = "ok",
    error_code: str = "",
) -> bool:
    """落一行脱敏后的决策轨迹；失败返回 False，绝不影响主回复。"""
    try:
        from app.core.memory import normalize_user_id

        uid = normalize_user_id(user_id)
        safe_query = sanitize(str(query or ""))[:500]
        safe_routing = _safe_routing(routing)
        safe_retrieval = _summary_map(
            retrieval,
            allowed={
                "memory_candidates", "memory_selected", "knowledge_candidates",
                "knowledge_selected", "entity_hits", "healed_chunks", "intent_label",
                "memories_used", "anchors_count", "expanded", "original_query_chars",
                "search_query_chars", "healer_words_count",
            },
            numeric={
                "memory_candidates", "memory_selected", "knowledge_candidates",
                "knowledge_selected", "entity_hits", "healed_chunks", "memories_used",
                "anchors_count", "original_query_chars", "search_query_chars",
                "healer_words_count",
            },
        )
        safe_healer = [_safe_text(word, 80) for word in (healer_words or [])[:20]]
        safe_injection = _summary_map(
            injection_bytes,
            allowed={"knowledge", "entity", "healed", "system_total"},
            numeric={"knowledge", "entity", "healed", "system_total"},
        )
        safe_stages = _safe_stages(stages)
        safe_reflection = _summary_map(
            reflection,
            allowed={"status", "review_ms", "revise_ms", "quality", "revision_count", "triggers"},
            numeric={"review_ms", "revise_ms", "revision_count"},
        )
        safe_trace_id = _safe_text(trace_id, 160)
        safe_request_id = _safe_text(request_id, 160)
        safe_channel = _safe_text(channel or "chat", 40)
        safe_route = _safe_text(route_name or "/api/chat", 160)
        safe_status = _safe_text(status or "ok", 32)
        safe_error = _safe_text(error_code, 80)
        conn = connect()
        try:
            conn.execute(
                """INSERT INTO request_traces
                   (trace_id, request_id, user_id, channel, route_name, query, ts,
                    routing, retrieval, retrieval_path, vector_degraded, healer,
                    injection_bytes, stages, reflection, search_ms, total_latency_ms, status, error_code)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    safe_trace_id,
                    safe_request_id,
                    uid,
                    safe_channel,
                    safe_route,
                    safe_query,
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(safe_routing, ensure_ascii=False),
                    json.dumps(safe_retrieval, ensure_ascii=False),
                    str(retrieval_path or "")[:40],
                    1 if vector_degraded else 0,
                    json.dumps(safe_healer, ensure_ascii=False),
                    json.dumps(safe_injection, ensure_ascii=False),
                    json.dumps(safe_stages, ensure_ascii=False),
                    json.dumps(safe_reflection, ensure_ascii=False),
                    max(0, int(search_ms or 0)),
                    max(0, int(total_latency_ms or 0)),
                    safe_status,
                    safe_error,
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
