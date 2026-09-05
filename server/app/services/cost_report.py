"""成本画像（P4）：LLM token 记账 × 决策轨迹 → 每轮/每路径成本报表。

request_traces 按调用主体聚合；LLM 用量在持久化层接入后也按同一主体返回。
"""
import json
from datetime import datetime, timedelta, timezone

from app.core import llm
from app.core.memory import _user_scope, normalize_user_id
from app.models.database import connect
from app.services.llm_usage import window_totals

MAX_TRACE_SCAN = 5000  # 注入体积均值最多扫描的轨迹行数（防超大窗口拖慢）


def cost_report(days: int = 7, user_id: str | None = None) -> dict:
    """返回指定用户范围内的 {llm, traces, days}。"""
    days = max(1, min(int(days), 90))
    uid = normalize_user_id(user_id)
    clause, args = _user_scope(uid, col="user_id")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    conn = connect()
    try:
        by_path = [dict(row) for row in conn.execute(
            f"""SELECT retrieval_path AS path, COUNT(*) AS n, AVG(search_ms) AS avg_ms
               FROM request_traces WHERE ts >= ? AND {clause}
               GROUP BY retrieval_path ORDER BY n DESC""",
            (cutoff, *args),
        ).fetchall()]
        total = conn.execute(
            f"SELECT COUNT(*) AS c, AVG(search_ms) AS m FROM request_traces WHERE ts >= ? AND {clause}",
            (cutoff, *args),
        ).fetchone()
        # 注入体积：逐行解析 JSON 取 system_total（上限扫描防超长窗口）
        bytes_sum = 0
        bytes_n = 0
        for row in conn.execute(
            f"SELECT injection_bytes FROM request_traces WHERE ts >= ? AND {clause} LIMIT ?",
            (cutoff, *args, MAX_TRACE_SCAN),
        ).fetchall():
            try:
                payload = json.loads(row["injection_bytes"] or "{}")
                value = payload.get("system_total")
                if isinstance(value, (int, float)) and value > 0:
                    bytes_sum += int(value)
                    bytes_n += 1
            except (json.JSONDecodeError, TypeError):
                continue
    finally:
        conn.close()

    persistent_usage = window_totals(days, uid)
    return {
        "days": days,
        "user_id": uid,
        "llm": persistent_usage,
        "llm_process_snapshot": llm.get_usage(),
        "llm_observability": llm.get_usage_details(),
        "traces": {
            "total": total["c"],
            "avg_search_ms": round(total["m"] or 0, 1),
            "avg_injection_bytes": round(bytes_sum / bytes_n, 1) if bytes_n else 0,
            "by_path": [
                {"path": row["path"] or "(未记录)", "n": row["n"],
                 "avg_ms": round(row["avg_ms"] or 0, 1)}
                for row in by_path
            ],
        },
    }
