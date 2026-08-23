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
]

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

本周事实：
{facts}

现有画像：
{profile}
"""


async def refresh_profile() -> dict:
    """读取本周 facts + 现有画像 → LLM 输出更新 → 写回。"""
    conn = connect()
    try:
        week_start = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        facts = conn.execute(
            "SELECT subject, predicate, object FROM facts WHERE updated_at >= ? LIMIT 100",
            (week_start,),
        ).fetchall()
        existing = conn.execute("SELECT dimension, value, confidence FROM profile").fetchall()
    finally:
        conn.close()

    if not facts:
        return {"updated": 0}

    facts_text = "\n".join(f"{r['subject']} {r['predicate']} {r['object']}" for r in facts)
    profile_text = "\n".join(f"[{r['dimension']}] {r['value']} (conf={r['confidence']})" for r in existing) or "（空）"

    result = await llm.chat_json(
        "你是用户画像分析师，只输出 JSON。",
        REFLECT_PROMPT.replace("{facts}", facts_text).replace("{profile}", profile_text),
    )

    now = datetime.now(timezone.utc).isoformat()
    updated = 0
    conn = connect()
    try:
        for u in result.get("updates", []):
            dim = u.get("dimension", "")
            if dim not in DIMENSIONS:
                continue
            conn.execute(
                """INSERT INTO profile (dimension, value, confidence, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(dimension) DO UPDATE
                   SET value=excluded.value, confidence=excluded.confidence, updated_at=excluded.updated_at""",
                (dim, u.get("value", ""), float(u.get("confidence", 0.5)), now),
            )
            updated += 1
        conn.commit()
    finally:
        conn.close()
    return {"updated": updated}


def get_profile_injection() -> str:
    """返回注入 prompt 的画像文本。"""
    conn = connect()
    try:
        rows = conn.execute("SELECT dimension, value FROM profile WHERE confidence >= 0.5").fetchall()
    finally:
        conn.close()
    if not rows:
        return ""
    lines = [f"[{r['dimension']}] {r['value']}" for r in rows]
    return "\n".join(lines)
