"""LLM 用量持久化：只保存统计元数据，不保存提示词或密钥。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.memory import _user_scope, normalize_user_id
from app.models.database import connect


def logical_request_id(task: str, user_id: str | None = None, scope: str = "") -> str:
    """为无 HTTP 请求的内部任务生成稳定、可追踪且不含敏感内容的逻辑 ID。"""
    uid = normalize_user_id(user_id)
    task = "-".join(str(task or "task").strip().split())[:60] or "task"
    scope = "-".join(str(scope or "run").strip().split())[:80] or "run"
    return f"{task}:{uid}:{scope}"[:160]


def record(
    *,
    request_id: str,
    user_id: str | None,
    model: str,
    key_index: int,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    fallback_count: int,
) -> bool:
    """持久化一条 LLM 用量记录；返回是否成功。"""
    uid = normalize_user_id(user_id)
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO llm_usage "
            "(user_id, request_id, model, key_index, prompt_tokens, completion_tokens, "
            "cached_tokens, fallback_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uid,
                str(request_id or "")[:160],
                str(model or "")[:200],
                max(0, int(key_index)),
                max(0, int(prompt_tokens)),
                max(0, int(completion_tokens)),
                max(0, int(cached_tokens)),
                max(0, int(fallback_count)),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def window_totals(days: int = 7, user_id: str | None = None) -> dict:
    """返回指定用户窗口内的持久化累计用量。"""
    days = max(1, min(int(days), 90))
    uid = normalize_user_id(user_id)
    clause, args = _user_scope(uid, col="user_id")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = connect()
    try:
        row = conn.execute(
            f"""SELECT COUNT(*) AS calls,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt,
                       COALESCE(SUM(completion_tokens), 0) AS completion,
                       COALESCE(SUM(cached_tokens), 0) AS cached,
                       COALESCE(SUM(fallback_count), 0) AS fallback_count
                FROM llm_usage WHERE created_at >= ? AND {clause}""",
            (cutoff, *args),
        ).fetchone()
    finally:
        conn.close()
    return {
        "calls": int(row["calls"] or 0),
        "prompt": int(row["prompt"] or 0),
        "completion": int(row["completion"] or 0),
        "cached": int(row["cached"] or 0),
        "fallback_count": int(row["fallback_count"] or 0),
    }
