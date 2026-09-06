"""轻量数据仓储：API 层的 SQL 收口点。

分层目标：api/ 不再手写 SQL；``_user_scope`` 的 f-string 拼接封死在本模块
（调用方只传 uid，不接触 SQL 片段）。函数全部为同步实现，async 端点用
``asyncio.to_thread`` 调用——顺带把统计类端点的同步 SQLite 移出事件循环。
服务层存量 SQL 允许保留；新增数据访问必须走本模块或 db_connection()。
"""
from __future__ import annotations

from app.core.memory import _user_scope
from app.models.database import connect


def recent_memories(uid: str, limit: int = 30) -> list[dict]:
    """某用户最近的对话记忆（Web 端聊天历史加载）。"""
    clause, args = _user_scope(uid)
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT id, sender, content, ts FROM memories WHERE {clause} "
            "ORDER BY id DESC LIMIT ?",
            (*args, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in reversed(rows)]


def stats_summary_blocks(uid: str, since: str) -> dict:
    """stats/summary 的 5 个聚合一连接取完。

    behavior_events/work_log 是主人专属采集和台账；只有 memories 需要按
    用户列过滤，避免访客聊天进入主人统计。
    """
    clause, args = _user_scope(uid)
    conn = connect()
    try:
        apps = conn.execute(
            f"""SELECT name, SUM(CAST(julianday(end_ts)-julianday(start_ts) AS REAL)*86400) AS secs
               FROM behavior_events WHERE kind='app_usage' AND start_ts >= ? AND {clause}
               GROUP BY name ORDER BY secs DESC LIMIT 10""",
            (since, *args),
        ).fetchall()
        browsers = conn.execute(
            f"""SELECT name, COUNT(*) AS cnt FROM behavior_events
               WHERE kind='browser' AND start_ts >= ? AND {clause}
               GROUP BY name ORDER BY cnt DESC LIMIT 10""",
            (since, *args),
        ).fetchall()
        n_commit = conn.execute(
            f"SELECT COUNT(*) AS c FROM behavior_events WHERE kind='git_commit' AND start_ts >= ? AND {clause}",
            (since, *args),
        ).fetchone()["c"]
        n_msg = conn.execute(
            f"SELECT COUNT(*) AS c FROM memories WHERE ts >= ? AND {clause}",
            (since, *args),
        ).fetchone()["c"]
        n_log = conn.execute(
            f"SELECT COUNT(*) AS c FROM work_log WHERE created_at >= ? AND {clause}",
            (since, *args),
        ).fetchone()["c"]
    finally:
        conn.close()
    return {
        "apps": [dict(a) for a in apps],
        "browsers": [dict(b) for b in browsers],
        "n_commit": n_commit,
        "n_msg": n_msg,
        "n_log": n_log,
    }


def hourly_memory_counts(uid: str, since: str) -> list[dict]:
    """按小时统计某用户的对话条数（chat 活跃时段分布）。"""
    clause, args = _user_scope(uid)
    conn = connect()
    try:
        rows = conn.execute(
            f"""SELECT CAST(strftime('%H', ts) AS INTEGER) AS h, COUNT(*) AS c
               FROM memories WHERE ts >= ? AND {clause} GROUP BY h""",
            (since, *args),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def list_weekly_reports(uid: str, limit: int = 20) -> list[dict]:
    clause, args = _user_scope(uid)
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT week, created_at FROM weekly_reports WHERE {clause} "
            "ORDER BY week DESC, id DESC LIMIT ?",
            (*args, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_weekly_report(uid: str, week: str) -> dict | None:
    clause, args = _user_scope(uid)
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT week, content, stats, created_at FROM weekly_reports "
            f"WHERE week=? AND {clause}",
            (week, *args),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def latest_daily_summary(uid: str) -> dict | None:
    clause, args = _user_scope(uid)
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT date, content, created_at FROM daily_summaries WHERE {clause} "
            "ORDER BY date DESC, id DESC LIMIT 1",
            args,
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def executed_commands_since(since_id: int) -> list[dict]:
    """id > since_id 的已执行指令（桌面端轮询显示执行结果）。"""
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT id, action, target, status, result FROM executor_commands
               WHERE id > ? AND status IN ('done', 'failed') ORDER BY id""",
            (since_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def insert_behavior_events(rows: list[dict]) -> int:
    """批量幂等插入行为事件（rows 已脱敏并含 event_id；采集器批量推送入口）。"""
    conn = connect()
    inserted = 0
    try:
        for r in rows:
            cur = conn.execute(
                """INSERT INTO behavior_events
                   (user_id, event_id, kind, name, detail, start_ts, end_ts, meta)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT DO NOTHING""",
                (r["user_id"], r["event_id"], r["kind"], r["name"], r["detail"],
                 r["start_ts"], r["end_ts"], r["meta"]),
            )
            inserted += max(0, cur.rowcount)
        conn.commit()
    finally:
        conn.close()
    return inserted
