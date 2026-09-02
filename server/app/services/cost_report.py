"""成本画像（P4）：LLM token 记账 × 决策轨迹 → 每轮/每路径成本报表。

不新增任何埋点——llm.py 的进程内 usage 计数器与 request_traces 表
（P0）已覆盖所需数据，这里只做聚合与呈现。
"""
import json
from datetime import datetime, timedelta, timezone

from app.core import llm
from app.models.database import connect

MAX_TRACE_SCAN = 5000  # 注入体积均值最多扫描的轨迹行数（防超大窗口拖慢）


def cost_report(days: int = 7) -> dict:
    """返回 {llm, traces, days}。

    llm：进程级累计（调用次数/prompt/completion/cached token）。
    traces：窗口内按检索路径的请求数、检索耗时均值、注入体积均值。
    """
    days = max(1, min(int(days), 90))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    conn = connect()
    try:
        by_path = [dict(r) for r in conn.execute(
            """SELECT retrieval_path AS path, COUNT(*) AS n, AVG(search_ms) AS avg_ms
               FROM request_traces WHERE ts >= ?
               GROUP BY retrieval_path ORDER BY n DESC""",
            (cutoff,),
        ).fetchall()]
        total = conn.execute(
            "SELECT COUNT(*) AS c, AVG(search_ms) AS m FROM request_traces WHERE ts >= ?",
            (cutoff,),
        ).fetchone()
        # 注入体积：逐行解析 JSON 取 system_total（上限扫描防超长窗口）
        bytes_sum = 0
        bytes_n = 0
        for r in conn.execute(
            "SELECT injection_bytes FROM request_traces WHERE ts >= ? LIMIT ?",
            (cutoff, MAX_TRACE_SCAN),
        ).fetchall():
            try:
                payload = json.loads(r["injection_bytes"] or "{}")
                v = payload.get("system_total")
                if isinstance(v, (int, float)) and v > 0:
                    bytes_sum += int(v)
                    bytes_n += 1
            except (json.JSONDecodeError, TypeError):
                continue
    finally:
        conn.close()

    return {
        "days": days,
        "llm": llm.get_usage(),
        "traces": {
            "total": total["c"],
            "avg_search_ms": round(total["m"] or 0, 1),
            "avg_injection_bytes": round(bytes_sum / bytes_n, 1) if bytes_n else 0,
            "by_path": [
                {"path": p["path"] or "(未记录)", "n": p["n"],
                 "avg_ms": round(p["avg_ms"] or 0, 1)}
                for p in by_path
            ],
        },
    }
