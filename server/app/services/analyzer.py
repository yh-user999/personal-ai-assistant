"""行为分析：纯 SQL 统计（无 LLM）+ 淘汰任务。

统计维度：
- 应用使用时长 Top / 按时段分布
- 浏览器域名话题分布
- git 提交频率/时段
- 工作日志汇总
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from app.models.database import connect


def weekly_stats(days: int = 7) -> dict:
    """周报用的统计摘要（键值对，直接给 LLM）。"""
    from app.core.memory import owner_user_id
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = connect()
    try:
        # 应用使用时长 Top5
        apps = conn.execute(
            """SELECT name, SUM(CAST(julianday(end_ts) - julianday(start_ts) AS REAL) * 86400) AS secs
               FROM behavior_events WHERE kind='app_usage' AND start_ts >= ?
               GROUP BY name ORDER BY secs DESC LIMIT 5""",
            (since,),
        ).fetchall()
        # 浏览器域名 Top5
        browsers = conn.execute(
            """SELECT name, COUNT(*) AS cnt FROM behavior_events
               WHERE kind='browser' AND start_ts >= ? GROUP BY name ORDER BY cnt DESC LIMIT 5""",
            (since,),
        ).fetchall()
        # git 提交数
        commits = conn.execute(
            "SELECT COUNT(*) AS cnt FROM behavior_events WHERE kind='git_commit' AND start_ts >= ?",
            (since,),
        ).fetchone()["cnt"]
        # 对话条数（主人专属统计，v0.4 不含访客）
        msgs = conn.execute(
            "SELECT COUNT(*) AS cnt FROM memories WHERE ts >= ? AND user_id IN (?, '')",
            (since, owner_user_id()),
        ).fetchone()["cnt"]
    finally:
        conn.close()

    return {
        "本周对话条数": msgs,
        "本周git提交数": commits,
        "应用时长Top5": json.dumps(
            [{"app": a["name"], "hours": round((a["secs"] or 0) / 3600, 1)} for a in apps],
            ensure_ascii=False,
        ),
        "浏览域名Top5": json.dumps(
            [{"domain": b["name"], "次数": b["cnt"]} for b in browsers], ensure_ascii=False
        ),
        "本周热点话题Top5": json.dumps(top_topics(days=days, limit=5), ensure_ascii=False),
    }


def top_topics(days: int = 7, limit: int = 5) -> list[dict]:
    """热点话题：近 days 天 memories.topics 出现频次 Top-N（主人专属，v0.4）。"""
    from collections import Counter

    from app.core.memory import owner_user_id

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    counter: Counter = Counter()
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT topics FROM memories WHERE topics != '' AND topics != '[]' AND ts >= ? AND user_id IN (?, '')",
            (since, owner_user_id()),
        )
        for r in rows:
            try:
                for t in json.loads(r["topics"]):
                    counter[t] += 1
            except (json.JSONDecodeError, TypeError):
                continue
    finally:
        conn.close()
    return [{"topic": t, "count": c} for t, c in counter.most_common(limit)]


# 行为事件留存期（天）：按 kind 分别设定。
# 背景：旧实现只删 kind='manual'，而采集器实际产出的是 app_usage / browser /
# git_commit——manual 一条都没有，淘汰任务形同虚设。实测 9 天累积 3359 行，
# 一年约 13 万行，且 weekly_stats 对该表做 SUM(julianday(...)) 全表聚合，
# 会随时间持续变慢。
# 取值依据：周报/统计只看近 7 天，画像看近 30 天，故 app_usage/browser
# 保留 90 天足够；git_commit 是"做过什么"的长期线索，留 2 年。
BEHAVIOR_RETENTION_DAYS = {
    "manual": 30,
    "app_usage": 90,
    "browser": 90,
    "git_commit": 730,
    "collector_alert": 30,
}
DEFAULT_BEHAVIOR_RETENTION_DAYS = 180  # 未登记的新 kind 兜底，避免又一次"永不清理"


def _sweep_orphan_vectors(conn) -> int:
    """清理 memories 已不存在的残留向量与 FTS 行。

    vec0 与 fts5 都不是外键表，删 memories 不会级联。任何历史遗留或中断
    都会留下孤儿行——向量表里的孤儿会参与 KNN 计算，JOIN 后被丢弃，
    表现为"检索召回数莫名偏少"。
    """
    removed = 0
    try:
        orphan_vec = [
            r["memory_id"]
            for r in conn.execute(
                "SELECT v.memory_id FROM memory_vectors v "
                "LEFT JOIN memories m ON m.id = v.memory_id WHERE m.id IS NULL"
            ).fetchall()
        ]
        for mid in orphan_vec:
            conn.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (mid,))
        removed += len(orphan_vec)
    except sqlite3.OperationalError:
        pass  # sqlite-vec 未安装
    try:
        removed += conn.execute(
            "DELETE FROM memories_fts WHERE memory_id NOT IN (SELECT id FROM memories)"
        ).rowcount
    except sqlite3.OperationalError:
        pass
    return removed


async def evict_stale() -> dict:
    """淘汰：行为事件按 kind 分留存期；超 365 天未再引用的低 importance 记忆删除。

    （6.22 课：chat 留存从 30 天放宽到 365 天——"记得聊过的每句话"要求
    旧对话多留一年；importance≥1 的记忆永不淘汰，重要设定早进 facts 层。）
    删除行同步清理 memories_fts 索引（FTS 无外键，残留行会污染检索）。
    """
    conn = connect()
    try:
        now = datetime.now(timezone.utc)
        # 行为事件：按 kind 各自的留存期清理（含库中出现过但未登记的 kind）
        kinds = [
            r["kind"]
            for r in conn.execute("SELECT DISTINCT kind FROM behavior_events").fetchall()
        ]
        n1 = 0
        for kind in kinds:
            days = BEHAVIOR_RETENTION_DAYS.get(kind, DEFAULT_BEHAVIOR_RETENTION_DAYS)
            cutoff = (now - timedelta(days=days)).isoformat()
            n1 += conn.execute(
                "DELETE FROM behavior_events WHERE kind=? AND start_ts < ?",
                (kind, cutoff),
            ).rowcount
        chat_cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
        stale_ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM memories WHERE ts < ? AND importance < 1.0", (chat_cutoff,)
            ).fetchall()
        ]
        # v0.4 访客记忆更激进：30 天直接删（不限 importance——访客确认过的
        # 设定已进 facts 层永久保留，原始聊天流水不必长留；也防陌生人灌库）
        from app.core.memory import owner_user_id

        guest_cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        guest_ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM memories WHERE ts < ? AND user_id NOT IN (?, '')",
                (guest_cutoff, owner_user_id()),
            ).fetchall()
        ]
        stale_ids = list(dict.fromkeys(stale_ids + guest_ids))
        if stale_ids:
            from app.core.memory import _fts_delete

            for mid in stale_ids:
                _fts_delete(conn, mid)
                try:
                    conn.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (mid,))
                except sqlite3.OperationalError:
                    pass  # sqlite-vec 未安装时基础功能仍可淘汰记忆
        n2 = conn.execute(
            "DELETE FROM memories WHERE id IN ({})".format(",".join("?" * len(stale_ids))),
            stale_ids,
        ).rowcount if stale_ids else 0
        # FTS5 不是外键表，先删索引再删 memories，避免孤儿索引长期膨胀。
        # 兜底清理孤儿向量：memories 已删而向量残留（历史删除路径未同步，
        # 或进程在两次 DELETE 之间中断）。线上实测存在 1 条这类残留。
        n3 = _sweep_orphan_vectors(conn)
        conn.commit()
        return {"deleted_noise": n1, "deleted_chat": n2, "deleted_orphan_vectors": n3}
    finally:
        conn.close()
