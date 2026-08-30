"""Goal 系统：用户的目标与进度管理。

命令（对话式）：
- "目标：XXX" / "新目标 XXX" → 创建活跃目标
- "目标完成：XXX" / "完成目标 XXX" → 标记 done
- "目标进度：XXX" / "XXX 做到第 5 周了" → 更新 progress
注入：活跃目标每次进 system prompt；周报核对目标。
"""
import re
from datetime import datetime, timezone

from app.models.database import connect


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_goal_command(msg: str) -> tuple[str, str] | None:
    """解析目标命令 → (action, payload)。action: create/done/progress。"""
    m = re.match(r"^(?:新目标|设定目标|目标)[:：]\s*(.+)$", msg.strip())
    if m:
        return ("create", m.group(1).strip()[:80])
    m = re.match(r"^(?:目标完成|完成目标)[:：]?\s*(.+)$", msg.strip())
    if m:
        return ("done", m.group(1).strip()[:80])
    m = re.match(r"^目标进度[:：]\s*(.+)$", msg.strip())
    if m:
        return ("progress", m.group(1).strip()[:120])
    return None


def add_goal(title: str) -> int:
    from app.services.sanitize import sanitize
    title = sanitize(title)
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO goals (title, created_at, updated_at) VALUES (?, ?, ?)",
            (title, _now(), _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def complete_goal(title: str) -> bool:
    """按标题模糊匹配最近活跃目标标记完成。"""
    conn = connect()
    try:
        cur = conn.execute(
            """UPDATE goals SET status='done', updated_at=?
               WHERE status='active' AND title LIKE ? AND id = (
                 SELECT id FROM goals WHERE status='active' AND title LIKE ?
                 ORDER BY id DESC LIMIT 1)""",
            (_now(), f"%{title}%", f"%{title}%"),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def update_progress(title_or_text: str) -> bool:
    """"XX 做到第5周" 或 "目标进度：XX" → 更新最近活跃目标进度。"""
    from app.services.sanitize import sanitize
    title_or_text = sanitize(title_or_text)
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, title FROM goals WHERE status='active' ORDER BY id DESC LIMIT 1"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return False
    target_id = rows[0]["id"]
    # 若文本含具体目标名，匹配对应目标
    for r in rows:
        if r["title"] in title_or_text:
            target_id = r["id"]
            break
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE goals SET progress=?, updated_at=? WHERE id=?",
            (title_or_text[:200], _now(), target_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_goals_injection() -> str:
    """活跃目标注入（含进度）。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT title, progress FROM goals WHERE status='active' ORDER BY id DESC LIMIT 5"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return ""
    parts = [f"- {r['title']}" + (f"（进度：{r['progress']}）" if r["progress"] else "") for r in rows]
    return "\n".join(parts)


def get_all_goals_text() -> str:
    """全部目标（周报核对用，含状态）。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT title, status, progress FROM goals ORDER BY id DESC LIMIT 10"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return "（无目标记录）"
    status_map = {"active": "进行中", "done": "已完成", "paused": "暂停"}
    return "\n".join(
        f"- [{status_map.get(r['status'], r['status'])}] {r['title']}"
        + (f"（{r['progress']}）" if r["progress"] else "")
        for r in rows
    )
