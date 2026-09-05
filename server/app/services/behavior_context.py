"""行为上下文：把采集到的行为实时注入聊天（聊天少也能懂用户）。

数据源：behavior_events（采集器）。全部为轻量 SQL 查询，无新 API 调用。
防护：陈旧数据不注入 / "今天"按北京时间 / 空值兜底 / 敏感信息再截断。
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.models.database import connect

TZ = ZoneInfo("Asia/Shanghai")
STALE_MINUTES = 10  # 窗口事件超过 10 分钟视为过时（采集器可能停了）


def _now() -> datetime:
    return datetime.now(TZ)


def _user_clause(user_id: str | None) -> tuple[str, tuple]:
    from app.core.memory import _user_scope, normalize_user_id

    return _user_scope(normalize_user_id(user_id), col="user_id")


def get_current_window(user_id: str | None = None) -> str | None:
    """最新 app_usage 事件；过时（>10 分钟）返回 None 防误导。"""
    clause, args = _user_clause(user_id)
    conn = connect()
    try:
        row = conn.execute(
            f"""SELECT name, detail, start_ts, end_ts FROM behavior_events
               WHERE kind='app_usage' AND {clause} ORDER BY id DESC LIMIT 1""",
            args,
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    # 新鲜度判断：
    # 有 end_ts（窗口已切换）→ 10 分钟阈值；
    # 无 end_ts（用户还停留在这个窗口）→ 放宽到 60 分钟（长驻窗口很常见）
    if row["end_ts"]:
        ref, limit_minutes = row["end_ts"], STALE_MINUTES
    else:
        ref, limit_minutes = row["start_ts"], 60
    try:
        if _now() - datetime.fromisoformat(ref) > timedelta(minutes=limit_minutes):
            return None
    except (TypeError, ValueError):
        return None
    app = (row["name"] or "unknown").strip() or "unknown"
    detail = (row["detail"] or "").strip()[:60]  # 注入前再截断，防敏感长标题
    return f"{app}（{detail}）" if detail else app


def get_today_commits(user_id: str | None = None) -> str | None:
    """今天（北京时间）的 git 提交数与最近提交信息。"""
    day_start = _now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    clause, args = _user_clause(user_id)
    conn = connect()
    try:
        n = conn.execute(
            f"SELECT COUNT(*) AS c FROM behavior_events WHERE kind='git_commit' AND start_ts >= ? AND {clause}",
            (day_start, *args),
        ).fetchone()["c"]
        last = conn.execute(
            f"""SELECT name, detail FROM behavior_events
               WHERE kind='git_commit' AND start_ts >= ? AND {clause}
               ORDER BY id DESC LIMIT 1""",
            (day_start, *args),
        ).fetchone()
    finally:
        conn.close()
    if not n:
        return None
    if last:
        return f"今天 git 提交 {n} 次（最近：{last['name']} · {(last['detail'] or '')[:40]}）"
    return f"今天 git 提交 {n} 次"


def get_recent_activity(hours: int = 1, user_id: str | None = None) -> str | None:
    """近 N 小时应用使用时长 Top3。"""
    since = (_now() - timedelta(hours=hours)).isoformat()
    clause, args = _user_clause(user_id)
    conn = connect()
    try:
        rows = conn.execute(
            f"""SELECT name,
                      SUM(CAST(julianday(end_ts)-julianday(start_ts) AS REAL)*3600) AS mins
               FROM behavior_events
               WHERE kind='app_usage' AND start_ts >= ? AND {clause}
               GROUP BY name ORDER BY mins DESC LIMIT 3""",
            (since, *args),
        ).fetchall()
    finally:
        conn.close()
    rows = [r for r in rows if (r["mins"] or 0) > 0]
    if not rows:
        return None
    parts = [f"{r['name']} {round(r['mins']):.0f}min" for r in rows]
    return f"近 {hours} 小时活跃：" + " / ".join(parts)


def get_behavior_injection(user_id: str | None = None) -> str:
    """组装当前用户行为上下文；全空返回空串。"""
    parts = []
    logger = logging.getLogger("assistant.behavior_context")
    for fn in (get_current_window, get_today_commits, get_recent_activity):
        try:
            text = fn(user_id=user_id)
            if text:
                parts.append(text)
        except Exception as exc:  # noqa: BLE001
            logger.debug("行为上下文采集失败: %s", exc)
            continue  # 行为注入失败不阻塞聊天（非关键路径）
    return "\n".join(parts)
