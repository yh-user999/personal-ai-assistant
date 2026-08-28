"""记忆闭环核心：
写入（消息入库 + 向量化 + 精确去重）、检索（向量 Top-K + 关键词兜底 + 主题活跃度补偿）、
注入（格式化 prompt 片段）。

v0.2 采纳外部评审优化：
- 精确去重：24h 内完全相同消息不重复入库
- 主题活跃度补偿：检索评分加入 topic boost（近 7 天高频话题不因时间衰减被淹没），
  对应 Generative Agents 的 recency/importance/relevance 三要素
参考 Wave Memory：importance 随引用增长、检索评分 = 相似度 × importance × 时间衰减。
"""
import json
import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from app.core import embedding
from app.models.database import connect

logger = logging.getLogger("assistant.memory")

INJECT_FORMAT = "[记忆] {ts}: {content}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 写入 ──────────────────────────────────────────────────

async def write_message(sender: str, content: str) -> int | None:
    """写入一条对话记忆并向量化。返回 memory_id（重复时返回 None）。

    入库前统一脱敏（手机号/邮箱/公网IP/自定义敏感词）——
    服务器不保存用户明文敏感信息。
    """
    from app.services.sanitize import sanitize

    content = sanitize(content)
    conn = connect()
    try:
        # 精确去重：24h 内完全相同内容不重复入库
        dup_cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        dup = conn.execute(
            "SELECT id FROM memories WHERE sender=? AND content=? AND ts >= ? LIMIT 1",
            (sender, content, dup_cutoff),
        ).fetchone()
        if dup:
            return None
        cur = conn.execute(
            "INSERT INTO memories (sender, content, ts, importance) VALUES (?, ?, ?, 1.0)",
            (sender, content, _now()),
        )
        memory_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    # 向量化（失败不阻塞写入）
    try:
        vec = (await embedding.embed([content]))[0]
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO memory_vectors (memory_id, embedding) VALUES (?, ?)",
                (memory_id, json.dumps(vec)),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        # 降级不阻塞写入，但必须留痕——否则 embedding key 失效会静默退化数周无人知晓
        logger.warning("记忆向量化失败，该条退化为关键词检索: %s", e)
    return memory_id


async def update_summary(memory_id: int, summary: str, topics: list[str]) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE memories SET summary = ?, topics = ? WHERE id = ?",
            (summary, json.dumps(topics, ensure_ascii=False), memory_id),
        )
        conn.commit()
    finally:
        conn.close()


# ── 检索 ──────────────────────────────────────────────────

def _topic_boost_map(conn, days: int = 7) -> Counter:
    """统计近 days 天各 topic 出现频次（用于热点补偿）。调用方负责关闭连接。"""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    counter: Counter = Counter()
    rows = conn.execute(
        "SELECT topics FROM memories WHERE topics != '' AND topics != '[]' AND ts >= ?",
        (since,),
    )
    for r in rows:
        try:
            for t in json.loads(r["topics"]):
                counter[t] += 1
        except (json.JSONDecodeError, TypeError):
            continue
    return counter


def _compute_topic_boost(topics_json: str, freq: Counter) -> float:
    """话题活跃度补偿：记忆所含话题近期越活跃，boost 越高（上限 1.5）。

    公式：1 + min(0.5, max_freq / 20)。单条记忆取所含话题的最高频次。
    """
    try:
        topics = json.loads(topics_json)
    except (json.JSONDecodeError, TypeError):
        return 1.0
    if not topics:
        return 1.0
    max_freq = max((freq.get(t, 0) for t in topics), default=0)
    return 1.0 + min(0.5, max_freq / 20.0)


async def search(query: str, top_k: int = 5, min_similarity: float = 0.35) -> list[dict]:
    """检索相关记忆。向量优先，失败退化为关键词。

    评分 = 相似度 × importance × 时间衰减 × 主题活跃度补偿
    返回: [{"id", "sender", "content", "summary", "ts", "topics", "score"}]
    """
    rows: list[dict] = []

    # 1) 向量检索（sqlite-vec vec0 是 KNN 虚拟表，必须 MATCH + k 语法，
    #    不能像普通列那样 WHERE v.distance < ?——距离过滤在 Python 层做）
    try:
        qvec = (await embedding.embed([query]))[0]
        conn = connect()
        try:
            cur = conn.execute(
                """
                SELECT m.id, m.sender, m.content, m.summary, m.ts, m.importance,
                       m.topics, v.distance
                FROM memory_vectors v
                JOIN memories m ON m.id = v.memory_id
                WHERE v.embedding MATCH ? AND k = ?
                """,
                (json.dumps(qvec), top_k * 2),
            )
            for r in cur.fetchall():
                rows.append(dict(r))
        finally:
            conn.close()
    except Exception as e:
        # 向量检索失败退化为关键词，但留痕排障（key 失效/服务宕机不该无声无息）
        logger.warning("向量检索失败，退化为关键词检索: %s", e)

    # 2) 关键词兜底（向量不可用或无结果时）
    if not rows:
        conn = connect()
        try:
            kw = f"%{query}%"
            cur = conn.execute(
                """
                SELECT id, sender, content, summary, ts, importance, topics, 0.5 AS distance
                FROM memories
                WHERE content LIKE ? OR summary LIKE ?
                ORDER BY ts DESC LIMIT ?
                """,
                (kw, kw, top_k),
            )
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    if not rows:
        return []

    # 3) 主题活跃度补偿
    conn = connect()
    try:
        freq = _topic_boost_map(conn)
    finally:
        conn.close()

    # 4) 综合评分 = 相似度 × importance × 时间衰减 × 话题补偿
    #    distance 为 cosine 距离（0~2）：sim = 1 - distance
    now = time.time()
    scored = []
    for r in rows:
        distance = r.get("distance")
        if distance is None:  # 零向量等特例（真实 embedding 不会出现）
            continue
        sim = max(0.0, 1.0 - float(distance))
        if sim < min_similarity:
            continue
        imp = float(r.get("importance", 1.0))
        try:
            age_days = (now - datetime.fromisoformat(r["ts"]).timestamp()) / 86400
        except Exception:
            age_days = 0
        decay = 0.5 ** (age_days / 30.0)  # 30 天半衰期
        boost = _compute_topic_boost(r.get("topics", ""), freq)
        scored.append({**r, "score": sim * imp * decay * boost})

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


def format_injection(memories: list[dict]) -> str:
    """格式化注入片段：[记忆] {日期}: {内容}"""
    if not memories:
        return ""
    parts = []
    for m in memories:
        ts = (m.get("ts") or "")[:10]
        content = m.get("content", "")
        if m.get("summary"):
            content = f"{m['summary']}（{content[:50]}）"
        parts.append(INJECT_FORMAT.format(ts=ts, content=content))
    return "\n".join(parts)


# ── importance 更新（被引用时 +0.02，上限 3.0）─────────────

def bump_importance(memory_ids: list[int]) -> None:
    conn = connect()
    try:
        for mid in memory_ids:
            conn.execute(
                "UPDATE memories SET importance = MIN(3.0, importance + 0.02) WHERE id = ?",
                (mid,),
            )
        conn.commit()
    finally:
        conn.close()


# ── 事实注入（v0.9：facts 三元组纳入每次聊天——"小月"失忆 bug 的系统性修复）──

def get_facts_injection(limit: int = 10) -> str:
    """最近 N 条持久事实（三元组），注入 prompt。身份/偏好/项目事实都在这里。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT subject, predicate, object FROM facts ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return ""
    return "\n".join(
        f"- {r['subject']} {r['predicate']} {r['object']}" for r in reversed(rows)
    )


# ── 多轮历史（v0.10：修复"单轮失忆"——"再确认一下"接不上上下文）──

def get_recent_history(limit: int = 8) -> list[dict]:
    """最近 N 条对话（正序），作为多轮上下文传给 LLM。每条截断 500 字符。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT sender, content FROM memories WHERE content != '' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"role": r["sender"], "content": r["content"][:500]}
        for r in reversed(rows)
    ]


def get_older_summaries(window_size: int = 8, limit: int = 4) -> list[str]:
    """窗口之外更早对话的摘要（正序），把"顺序感"续到 8 轮以后。

    优先级：summary（consolidation 已提炼）→ 原文短截断（4h 内尚未提炼的兜底）。
    零额外 LLM 成本。
    """
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT content, summary FROM memories
               WHERE content != '' ORDER BY id DESC LIMIT ?""",
            (window_size + limit * 6,),  # 多取一些，summary 可能为空/__merged__
        ).fetchall()
    finally:
        conn.close()
    summaries = []
    for r in rows[window_size:]:
        s = (r["summary"] or "").strip()
        if s and s != "__merged__":
            summaries.append(s)
        elif r["content"]:
            # 兜底：4h 内未提炼的消息用原文短截断续上
            summaries.append(r["content"][:120])
        if len(summaries) >= limit:
            break
    return list(reversed(summaries))  # 正序（老→新）
