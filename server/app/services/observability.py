"""本地只读可观测查询：只返回脱敏后的 Trace 元数据与聚合统计。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.memory import _user_scope, normalize_user_id
from app.models.database import connect
from app.services.sanitize import sanitize

MAX_LIMIT = 200
MAX_OFFSET = 100_000
MAX_SCAN = 10_000

_TRACE_COLUMNS = (
    "id, trace_id, request_id, user_id, channel, route_name, query, ts, "
    "retrieval_path, vector_degraded, total_latency_ms, status, error_code, "
    "routing, retrieval, healer, injection_bytes, stages, search_ms, response_plan, reflection"
)


def _clamp_page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(int(limit), MAX_LIMIT)), max(0, min(int(offset), MAX_OFFSET))


def _cutoff(days: int) -> tuple[int, str]:
    safe_days = max(1, min(int(days), 90))
    return safe_days, (datetime.now(timezone.utc) - timedelta(days=safe_days)).isoformat()


def _parse_json(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed


def _public_summary(value: Any, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in allowed:
        if key not in value:
            continue
        raw = value[key]
        result[key] = sanitize(raw)[:160] if isinstance(raw, str) else raw
    return result


def _public_stages(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for name, raw in list(value.items())[:20]:
        if not isinstance(raw, dict):
            continue
        item = {}
        if raw.get("status"):
            item["status"] = sanitize(str(raw["status"]))[:24]
        if isinstance(raw.get("elapsed_ms"), (int, float)):
            item["elapsed_ms"] = max(0, int(raw["elapsed_ms"]))
        if raw.get("error"):
            item["error"] = sanitize(str(raw["error"]))[:80]
        result[sanitize(str(name))[:48]] = item
    return result


def _row_payload(row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "trace_id": sanitize(str(row["trace_id"] or ""))[:160],
        "request_id": sanitize(str(row["request_id"] or ""))[:160],
        "user_id": sanitize(str(row["user_id"] or ""))[:32],
        "channel": sanitize(str(row["channel"] or ""))[:40],
        "route_name": sanitize(str(row["route_name"] or ""))[:160],
        "query": sanitize(str(row["query"] or ""))[:500],
        "ts": sanitize(str(row["ts"] or ""))[:40],
        "retrieval_path": sanitize(str(row["retrieval_path"] or ""))[:40],
        "vector_degraded": bool(row["vector_degraded"]),
        "total_latency_ms": max(0, int(row["total_latency_ms"] or 0)),
        "status": sanitize(str(row["status"] or "ok"))[:32],
        "error_code": sanitize(str(row["error_code"] or ""))[:80],
        "routing": _public_summary(_parse_json(row["routing"], {}), {"domains", "docs"}),
        "retrieval": _public_summary(
            _parse_json(row["retrieval"], {}),
            {
                "memory_candidates", "memory_selected", "knowledge_candidates",
                "knowledge_selected", "entity_hits", "healed_chunks", "intent_label",
                "memories_used", "anchors_count", "expanded", "original_query_chars",
                "search_query_chars", "healer_words_count",
            },
        ),
        "healer": [sanitize(str(item))[:80] for item in (_parse_json(row["healer"], []) or [])[:20]],
        "injection_bytes": _public_summary(
            _parse_json(row["injection_bytes"], {}),
            {"knowledge", "entity", "healed", "system_total"},
        ),
        "stages": _public_stages(_parse_json(row["stages"], {})),
        "search_ms": max(0, int(row["search_ms"] or 0)),
        # API只公开统计与停止原因；不返回调查摘要、来源原文或模型自由输出。
        "investigation": _public_summary(_parse_json(row["response_plan"], {}), {
            "investigation_status", "investigation_rounds", "investigation_search_calls",
            "investigation_pages_read", "investigation_claim_count", "investigation_gap_count",
            "investigation_elapsed_ms", "investigation_stop_reason", "investigation_resumed", "investigation_analysis_error",
        }),
        "reflection": _public_summary(_parse_json(row["reflection"], {}), {
            "status", "review_ms", "revise_ms", "revision_count", "revision_status", "safety_fallback",
        }),
    }


def _where(
    uid: str,
    since: str,
    *,
    trace_id: str = "",
    request_id: str = "",
    query: str = "",
    status: str = "",
    channel: str = "",
    retrieval_path: str = "",
) -> tuple[str, list[Any]]:
    scope_clause, scope_args = _user_scope(uid, col="user_id")
    clauses = ["ts >= ?", scope_clause]
    args: list[Any] = [since, *scope_args]
    filters = (
        ("trace_id", trace_id),
        ("request_id", request_id),
        ("status", status),
        ("channel", channel),
        ("retrieval_path", retrieval_path),
    )
    for column, value in filters:
        if value:
            clauses.append(f"{column} = ?")
            args.append(str(value)[:160])
    if query:
        clauses.append("query LIKE ?")
        args.append(f"%{str(query)[:200]}%")
    return " AND ".join(clauses), args


def list_traces(
    user_id: str | None,
    *,
    days: int = 7,
    limit: int = 50,
    offset: int = 0,
    trace_id: str = "",
    request_id: str = "",
    query: str = "",
    status: str = "",
    channel: str = "",
    retrieval_path: str = "",
) -> dict[str, Any]:
    """分页查询指定主体的 Trace；不返回 prompt、图片、token 或行为全文。"""
    uid = normalize_user_id(user_id)
    limit, offset = _clamp_page(limit, offset)
    days, since = _cutoff(days)
    where, args = _where(
        uid,
        since,
        trace_id=trace_id,
        request_id=request_id,
        query=query,
        status=status,
        channel=channel,
        retrieval_path=retrieval_path,
    )
    conn = connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM request_traces WHERE {where}", args
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT {_TRACE_COLUMNS} FROM request_traces WHERE {where} "
            "ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()
    finally:
        conn.close()
    return {
        "days": days,
        "limit": limit,
        "offset": offset,
        "total": int(total or 0),
        "items": [_row_payload(row) for row in rows],
    }


def get_trace(user_id: str | None, trace_id: str) -> dict[str, Any] | None:
    """按 trace_id 读取单条 Trace，并再次按主体范围过滤。"""
    uid = normalize_user_id(user_id)
    scope_clause, scope_args = _user_scope(uid, col="user_id")
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT {_TRACE_COLUMNS} FROM request_traces "
            f"WHERE trace_id=? AND {scope_clause} LIMIT 1",
            (str(trace_id or "")[:160], *scope_args),
        ).fetchone()
    finally:
        conn.close()
    return _row_payload(row) if row else None


def summary(user_id: str | None, *, days: int = 7) -> dict[str, Any]:
    """返回只含统计值的观测摘要。"""
    uid = normalize_user_id(user_id)
    days, since = _cutoff(days)
    scope_clause, scope_args = _user_scope(uid, col="user_id")
    args = (since, *scope_args)
    conn = connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS c, "
            f"SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, "
            f"SUM(CASE WHEN vector_degraded=1 THEN 1 ELSE 0 END) AS degraded, "
            f"AVG(total_latency_ms) AS latency, AVG(search_ms) AS search "
            f"FROM request_traces WHERE ts >= ? AND {scope_clause}",
            args,
        ).fetchone()
        by_path = conn.execute(
            f"SELECT retrieval_path AS path, COUNT(*) AS n "
            f"FROM request_traces WHERE ts >= ? AND {scope_clause} "
            "GROUP BY retrieval_path ORDER BY n DESC LIMIT 20",
            args,
        ).fetchall()
        by_channel = conn.execute(
            f"SELECT channel, COUNT(*) AS n "
            f"FROM request_traces WHERE ts >= ? AND {scope_clause} "
            "GROUP BY channel ORDER BY n DESC LIMIT 20",
            args,
        ).fetchall()
        latency_rows = conn.execute(
            f"SELECT total_latency_ms FROM request_traces "
            f"WHERE ts >= ? AND {scope_clause} AND total_latency_ms >= 0 "
            "ORDER BY total_latency_ms LIMIT ?",
            (*args, MAX_SCAN),
        ).fetchall()
    finally:
        conn.close()

    values = [int(row["total_latency_ms"] or 0) for row in latency_rows]

    def percentile(percent: float) -> int:
        if not values:
            return 0
        index = min(len(values) - 1, max(0, round((len(values) - 1) * percent)))
        return values[index]

    count = int(total["c"] or 0)
    failed = int(total["failed"] or 0)
    degraded = int(total["degraded"] or 0)
    return {
        "days": days,
        "requests": count,
        "failed": failed,
        "failure_rate": round(failed / count, 4) if count else 0,
        "degraded": degraded,
        "degraded_rate": round(degraded / count, 4) if count else 0,
        "latency_ms": {
            "avg": round(float(total["latency"] or 0), 1),
            "p50": percentile(0.50),
            "p95": percentile(0.95),
        },
        "search_ms_avg": round(float(total["search"] or 0), 1),
        "by_path": [{"path": row["path"] or "(未记录)", "count": row["n"]} for row in by_path],
        "by_channel": [{"channel": row["channel"] or "(未记录)", "count": row["n"]} for row in by_channel],
    }
