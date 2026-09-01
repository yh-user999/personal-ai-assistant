"""摘要整合服务：把最近一批碎片消息交给 LLM，产出
summary / topics / facts(三元组) / relations，写回 memories 并更新 facts 表。

思路照抄 Wave Memory services/consolidation.py 的 prompt 设计，去掉群聊维度。
"""
import json
from datetime import datetime, timedelta, timezone

from app.core import llm
from app.models.database import connect

CONSOLIDATION_PROMPT = """你是记忆整合系统，只输出 JSON。
把下面的对话片段整合为：
{
  "summary": "一句话概括这段对话的核心内容",
  "topics": ["话题1", "话题2", "话题3"],
  "facts": [{"subject": "主语(具体人名或专名)", "predicate": "谓语", "object": "宾语"}],
  "relations": ["话题A 与 话题B 的关系"]
}
要求：
- topics 最多 3 个，用简短名词短语
- facts 最多 5 个，必须是三元组格式
- 对话是「用户(本人) 与 AI 助手」的对话。用户消息中的"我/本人/咱们"一律映射为
  subject="用户"（例如 "我在做项目" → {"subject": "用户", "predicate": "在做", "object": "项目"}）；
  AI 助手的陈述不要作为 facts 主语
- 如果对话是无意义闲聊，summary 写"日常闲聊"，其他字段留空数组

对话片段：
{conversation}
"""


async def _consolidate_user(uid: str, since: str) -> int:
    """整合单个用户窗口内的未整合消息（v0.4 多人隔离版）。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, content, ts FROM memories WHERE user_id=? AND sender='user' "
            "AND summary='' AND ts >= ? ORDER BY ts LIMIT 50",
            (uid, since),
        ).fetchall()
        if not rows:
            return 0
        conversation = "\n".join(f"{r['ts'][:16]} {r['content']}" for r in rows)
        ids = [r["id"] for r in rows]
    finally:
        conn.close()

    result = await llm.chat_json(
        "你是记忆整合系统，只输出 JSON。",
        CONSOLIDATION_PROMPT.replace("{conversation}", conversation),
    )
    summary = result.get("summary", "")
    topics = result.get("topics", [])

    # 关切追踪：本批话题更新关切表（提及次数+1、刷新时间），限定当前用户
    from app.services.concern_tracker import upsert_concerns

    upsert_concerns(topics, user_id=uid)

    conn = connect()
    try:
        # 第一个消息作为代表写入 summary（其余置空避免重复整合）。
        # 走 update_summary_sync 而不是裸 UPDATE：摘要必须同步进 memories_fts，
        # 否则提炼出来的 summary 检索不到（BM25 与"更早对话摘要"都依赖它）。
        from app.core.memory import update_summary_sync

        update_summary_sync(conn, ids[0], summary, topics)
        # 只有一条消息时没有“其余消息”，跳过空 IN () 更新。
        if len(ids) > 1:
            conn.execute(
                "UPDATE memories SET summary='__merged__' WHERE id IN ({})".format(
                    ",".join("?" * (len(ids) - 1))
                ),
                ids[1:],
            )
        # facts 入库（去重：同三元组则更新 updated_at）
        now = datetime.now(timezone.utc).isoformat()
        for f in result.get("facts", []):
            subj, pred, obj = f.get("subject", ""), f.get("predicate", ""), f.get("object", "")
            if not (subj and pred and obj):
                continue
            conn.execute(
                """INSERT INTO facts (user_id, subject, predicate, object, source_memory_id, confidence, updated_at)
                   VALUES (?, ?, ?, ?, ?, 0.7, ?)
                   ON CONFLICT(user_id, subject, predicate, object)
                   DO UPDATE SET updated_at=excluded.updated_at""",
                (uid, subj, pred, obj, ids[0], now),
            )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


async def consolidate_recent(hours: int = 2) -> dict:
    """整合最近 hours 小时内、尚未整合的用户消息（按用户逐个隔离整合）。"""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM memories WHERE sender='user' AND summary='' AND ts >= ?",
            (since,),
        ).fetchall()
    finally:
        conn.close()
    users = [r["user_id"] for r in rows if r["user_id"]]
    total = 0
    for uid in users:
        total += await _consolidate_user(uid, since)
    return {"messages": total}
