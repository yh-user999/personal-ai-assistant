"""自省模块：检测用户纠正 → 存教训（长期记忆）→ 聊天时注入 → 周报统计。

对应身份定义行为规范："禁止忽视用户的历史选择和风格偏好"。
纠正信号词命中即视为纠正（简单可靠，不额外调 LLM）。
"""
from datetime import datetime, timezone

from app.models.database import connect

# 纠正信号（中文口语常见表达；命中任一词即判定）
# 含身份设定类："给你起名 X / 你叫 X / 你的名字是 X"
CORRECTION_PATTERNS = (
    "不对", "错了", "不是这样", "应该是", "记住", "以后", "纠正",
    "别说", "不要再", "别再说", "你应该", "要记住",
    "起名", "名字叫", "就叫你", "你的名字", "叫你",
)


def detect_correction(text: str) -> bool:
    """用户消息是否含纠正信号。"""
    return any(p in text for p in CORRECTION_PATTERNS)


def save_lesson(content: str, context: str = "") -> int:
    """存一条教训：用户原话 + 被纠正的 AI 回复上下文（统一脱敏）。"""
    from app.services.sanitize import sanitize
    content = sanitize(content)
    context = sanitize(context)
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO lessons (content, context, created_at) VALUES (?, ?, ?)",
            (content[:300], context[:300], datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_lessons_injection(limit: int = 5) -> str:
    """最近 N 条教训，注入 system prompt（越新的越靠后，权重感更强）。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT content FROM lessons ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return ""
    return "\n".join(f"- {r['content']}" for r in reversed(rows))


def count_lessons_since(since_iso: str) -> int:
    """统计某时间以来的教训数（周报用）。"""
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM lessons WHERE created_at >= ?", (since_iso,)
        ).fetchone()["c"]
    finally:
        conn.close()
    return n
