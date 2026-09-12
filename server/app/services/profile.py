"""画像服务：四维度画像（technical_background / work_habit / learning_rhythm / project_info）
每周用 LLM 从本周 facts 增量更新，带 confidence 与更新时间。

维度定义对应 docs/实施方案细则.md 与身份定义：
- technical_background: 编程语言、框架熟悉度、AI/数据方向
- work_habit: 提问时间、问题复杂度、偏好格式
- learning_rhythm: 快速答案 or 深度讲解、是否需要代码示例
- project_info: 当前项目、阶段目标、进度
"""
from datetime import datetime, timedelta, timezone

from app.core import llm
from app.models.database import connect

DIMENSIONS = [
    "technical_background",
    "work_habit",
    "learning_rhythm",
    "project_info",
    # 对话偏好：私聊与群聊共用。画像按 user_id（QQ 号）归属，
    # 同一个人在哪个场景说的都算他自己的画像，不再分表。
    "preferred_name",
    "topics",
    "style",
]

# 明确禁止的敏感维度：即使 LLM 返回也丢弃。群聊放开后素材来自第三方发言，
# 仅靠提示词约束不足以防注入或模型自作主张。
BLOCKED_DIMENSIONS = frozenset({
    "politics", "religion", "health", "sexual_orientation", "income",
    "address", "phone", "id_number", "family", "race", "ethnicity",
})

REFLECT_PROMPT = """你是用户画像分析师。基于本周提取的事实三元组与现有画像，输出：
{{
  "updates": [
    {{"dimension": "technical_background", "value": "更新后的画像描述", "confidence": 0.8}}
  ]
}}
要求：
- dimension 只能是: technical_background / work_habit / learning_rhythm / project_info
- 没有新信息支撑的维度不要输出
- confidence 0-1，信息直接且多次出现给高分
- 每条 value 不超过 80 字，只写有事实依据的结论，不得照抄事实原文
- 若某维度现状与已有画像一致，不要重复输出该维度

本周事实：
{facts}

现有画像：
{profile}
"""


async def refresh_profile(
    user_id: str | None = None,
    request_id: str | None = None,
) -> dict:
    """读取指定主体本周 facts + 现有画像 → LLM 输出更新 → 写回。"""
    from app.core.memory import normalize_user_id
    from app.services.llm_usage import logical_request_id

    uid = normalize_user_id(user_id)
    conn = connect()
    try:
        week_start = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        facts = conn.execute(
            "SELECT subject, predicate, object FROM facts WHERE user_id=? AND updated_at >= ? LIMIT 100",
            (uid, week_start),
        ).fetchall()
        existing = conn.execute(
            "SELECT dimension, value, confidence FROM profile WHERE user_id=?", (uid,)
        ).fetchall()
    finally:
        conn.close()

    if not facts:
        return {"updated": 0}

    facts_text = "\n".join(f"{r['subject']} {r['predicate']} {r['object']}" for r in facts)
    profile_text = "\n".join(f"[{r['dimension']}] {r['value']} (conf={r['confidence']})" for r in existing) or "（空）"

    week_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = await llm.chat_json(
        "你是用户画像分析师，只输出 JSON。",
        REFLECT_PROMPT.replace("{facts}", facts_text).replace("{profile}", profile_text),
        request_id=request_id or logical_request_id("profile_refresh", uid, week_key),
        user_id=uid,
    )

    now = datetime.now(timezone.utc).isoformat()
    updated = 0
    conn = connect()
    try:
        for u in result.get("updates", []):
            dim = u.get("dimension", "")
            if dim not in DIMENSIONS or dim in BLOCKED_DIMENSIONS:
                continue
            conn.execute(
                """INSERT INTO profile (user_id, dimension, value, confidence, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, dimension) DO UPDATE
                   SET value=excluded.value, confidence=excluded.confidence, updated_at=excluded.updated_at""",
                (uid, dim, u.get("value", ""), float(u.get("confidence", 0.5)), now),
            )
            updated += 1
        conn.commit()
    finally:
        conn.close()
    return {"updated": updated}


def get_profile_injection(user_id: str | None = None) -> str:
    """返回注入 prompt 的画像文本（v0.4：限定当前用户）。"""
    from app.core.memory import normalize_user_id

    uid = normalize_user_id(user_id)
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT dimension, value FROM profile WHERE user_id=? AND confidence >= 0.5", (uid,)
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return ""
    lines = [f"[{r['dimension']}] {r['value'][:120]}" for r in rows]
    return "\n".join(lines)


MAX_VALUE_CHARS = 200


def remember(user_id: str, dimension: str, value: str, confidence: float = 0.6) -> bool:
    """写入/更新一条画像。维度不在白名单或属敏感项则丢弃。

    画像按 user_id（QQ 号）归属：同一个人无论在私聊还是群里说的，
    都记到他自己名下，因此不需要按场景分表。
    """
    from app.core.memory import normalize_user_id

    dim = str(dimension or "").strip()
    text = " ".join(str(value or "").split())[:MAX_VALUE_CHARS]
    if not text or dim not in DIMENSIONS or dim in BLOCKED_DIMENSIONS:
        return False
    uid = normalize_user_id(user_id)
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO profile (user_id, dimension, value, confidence, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, dimension) DO UPDATE
                 SET value=excluded.value, confidence=excluded.confidence,
                     updated_at=excluded.updated_at""",
            (uid, dim, text, max(0.0, min(1.0, float(confidence))),
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def forget_user(user_id: str) -> int:
    """删除某人的全部画像（应对"别记我"的要求），返回删除条数。"""
    from app.core.memory import normalize_user_id

    conn = connect()
    try:
        cur = conn.execute(
            "DELETE FROM profile WHERE user_id=?", (normalize_user_id(user_id),)
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def parse_updates(payload: object) -> list[tuple[str, str, float]]:
    """解析 LLM 返回的画像更新，入库前收敛非法与敏感维度。

    模型输出不可信（尤其素材来自群聊时可能含注入），必须集中过滤。
    """
    import json as _json

    if isinstance(payload, str):
        try:
            payload = _json.loads(payload)
        except (TypeError, ValueError):
            return []
    if not isinstance(payload, dict):
        return []
    out: list[tuple[str, str, float]] = []
    for item in payload.get("updates", []) or []:
        if not isinstance(item, dict):
            continue
        dim = str(item.get("dimension", "")).strip()
        if dim not in DIMENSIONS or dim in BLOCKED_DIMENSIONS:
            continue
        value = " ".join(str(item.get("value", "")).split())[:MAX_VALUE_CHARS]
        if not value:
            continue
        try:
            conf = max(0.0, min(1.0, float(item.get("confidence", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        out.append((dim, value, conf))
    return out
