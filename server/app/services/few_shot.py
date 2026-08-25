"""风格学习：从用户满意的回复里提取风格范例，few-shot 注入。

检测正向反馈（"很好/不错/就这样/对，没错…"）→ 上一条 AI 回复即为风格范例。
注入：最近 2 条范例（自省管内容对错，风格管形式偏好）。
"""
import re

from app.models.database import connect

_POSITIVE_RE = re.compile(r"很好|不错|就这样|对，没错|正合我意|赞|比上次好|这个可以|很满意")


def detect_positive_feedback(text: str) -> bool:
    """用户消息是否是对上一条回复的认可。"""
    return bool(_POSITIVE_RE.search(text)) and len(text) < 80  # 短消息才可能是反馈


def save_example(content: str) -> int:
    """存一条风格范例（用户满意的 AI 回复）。"""
    from datetime import datetime, timezone

    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO style_examples (content, created_at) VALUES (?, ?)",
            (content[:400], datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_examples_injection(limit: int = 2) -> str:
    """最近 N 条风格范例，few-shot 注入。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT content FROM style_examples ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return ""
    return "\n\n".join(f"<风格范例> {r['content'][:250]}" for r in reversed(rows))
