"""一跳共现扩散：找出语义不相近但实际相关的记忆。

解决的场景：问"上次跳槽那事我怎么打算的"，向量检索只能捞到字面含"跳槽"
的记忆；而当时的思考可能记在「薪资对比」「通勤时间」「技术栈匹配」几条里，
它们语义上离查询很远，但**和跳槽出现在同一次对话里**。

这是 Wave Memory「脉冲传播」的极简版——只做一跳、不做虫洞/动量/内生残差。
那些是 44 万条边上的调优；本项目 700 条记忆量级，一跳足够，多跳只会引入噪声。

自动启用（关键设计）：
共现图需要"同一话题出现在多条记忆里"才有边。实测本地 719 条记忆里只有
9 条有 topics、22 个话题各出现 1 次——一条边都建不出来。所以按数据量门控：
达不到门槛就直接返回空，不做无用计算、也不引入随机噪声。
"""
import json
import logging
from collections import Counter, defaultdict

from app.models.database import connect

logger = logging.getLogger("assistant.cooccurrence")

# 启用门槛：带 topics 的记忆数与可用边数都要达标，否则扩散没有意义
MIN_TAGGED_MEMORIES = 200   # 带 topics 的记忆条数
MIN_SHARED_TOPICS = 20      # 至少出现在 2 条记忆里的话题数

# 单个话题关联的记忆数上限：超过这个数说明它是"日常闲聊"这类泛话题，
# 拿它连边等于把半个库连成一坨，反而稀释信号
MAX_MEMORIES_PER_TOPIC = 12

EXPAND_TOP_K = 3            # 最多补充几条
SCORE_PENALTY = 0.5         # 扩散来的记忆分数打折（不能压过直接命中）


def _load_topic_index(user_id: str) -> dict[str, list[int]]:
    """{话题: [记忆 id...]}，只取本用户的。"""
    from app.core.memory import _user_scope

    clause, args = _user_scope(user_id)
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT id, topics FROM memories WHERE topics IS NOT NULL "
            f"AND topics != '[]' AND {clause}",
            args,
        ).fetchall()
    finally:
        conn.close()
    index: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        try:
            topics = json.loads(r["topics"])
        except (TypeError, ValueError):
            continue
        for t in topics:
            t = (t or "").strip()
            if t:
                index[t].append(r["id"])
    return index


def is_enabled(user_id: str | None = None) -> tuple[bool, str]:
    """数据量是否够支撑共现扩散。返回 (可用, 原因)。"""
    from app.core.memory import normalize_user_id

    uid = normalize_user_id(user_id)
    index = _load_topic_index(uid)
    tagged = len({mid for ids in index.values() for mid in ids})
    shared = sum(1 for ids in index.values() if len(set(ids)) >= 2)
    if tagged < MIN_TAGGED_MEMORIES:
        return False, f"带话题的记忆仅 {tagged} 条（需 ≥{MIN_TAGGED_MEMORIES}）"
    if shared < MIN_SHARED_TOPICS:
        return False, f"共享话题仅 {shared} 个（需 ≥{MIN_SHARED_TOPICS}）"
    return True, ""


def expand(hits: list[dict], user_id: str | None = None,
           top_k: int = EXPAND_TOP_K) -> list[dict]:
    """基于命中记忆的话题，补充一跳关联记忆。

    数据量不足时原样返回——宁可不扩散，也不要在稀疏图上编造关联。
    """
    if not hits:
        return hits
    from app.core.memory import _user_scope, normalize_user_id

    uid = normalize_user_id(user_id)
    ok, _ = is_enabled(uid)
    if not ok:
        return hits

    index = _load_topic_index(uid)
    hit_ids = {h["id"] for h in hits if h.get("id") is not None}

    # 命中记忆涉及的话题
    seed_topics: set[str] = set()
    for topic, ids in index.items():
        if hit_ids & set(ids):
            if len(set(ids)) <= MAX_MEMORIES_PER_TOPIC:  # 跳过泛话题
                seed_topics.add(topic)

    # 候选：与种子话题共现、且不在已命中里；按共现话题数排序
    votes: Counter = Counter()
    for topic in seed_topics:
        for mid in set(index[topic]):
            if mid not in hit_ids:
                votes[mid] += 1
    if not votes:
        return hits

    picked = [mid for mid, _ in votes.most_common(top_k)]
    clause, args = _user_scope(uid)
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT id, content, summary, ts FROM memories "
            f"WHERE id IN ({','.join('?' * len(picked))}) AND {clause}",
            (*picked, *args),
        ).fetchall()
    finally:
        conn.close()

    base = min((h.get("score", 0.5) for h in hits), default=0.5)
    extra = [
        {
            "id": r["id"],
            "content": r["content"],
            "summary": r["summary"],
            "ts": r["ts"],
            "score": base * SCORE_PENALTY,
            "via_cooccurrence": True,
        }
        for r in rows
    ]
    if extra:
        logger.debug("共现扩散补充 %d 条", len(extra))
    return hits + extra
