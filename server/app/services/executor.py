"""执行器（第 11 课）：机器人操作 Windows 的指令队列。

流程：
  聊天命令"帮我打开XX/看看XX目录/读一下XX文件"
  → parse → enqueue（executor_commands 表）
  → Windows executor.py 每 5s 轮询 pending → 执行 → 回传 result
  → 服务器把 result 写为 assistant 消息（下次聊天可见）

安全分级：list_dir/read_file 限制在 EXECUTOR_ALLOWED_ROOTS 白名单内；
open 仅 startfile（打开文件/文件夹/应用，不执行命令）。
"""
import re
from datetime import datetime, timezone

from app.config import settings
from app.models.database import connect


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_executor_command(msg: str) -> tuple[str, str] | None:
    """解析操作命令 → (action, target)。action: open/list_dir/read_file。"""
    m = re.match(r"^(?:帮我|请)?打开(?:文件夹|目录|应用|软件)?[:：]?\s*(.+)$", msg.strip())
    if m:
        return ("open", m.group(1).strip()[:200])
    m = re.match(r"^(?:列出|看看|查看)(.+?)(?:目录|文件夹)(?:里)?(?:有什么|的内容)?$", msg.strip())
    if m:
        return ("list_dir", m.group(1).strip()[:200])
    m = re.match(r"^(?:帮我|请)?(?:看看|查看|读一下|读取)(?:文件)?[:：]?\s*(.+)$", msg.strip())
    if m and not m.group(1).endswith(("目录", "文件夹")):
        return ("read_file", m.group(1).strip()[:200])
    return None


def enqueue(action: str, target: str) -> int:
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO executor_commands (action, target, created_at) VALUES (?, ?, ?)",
            (action, target, _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_pending() -> dict | None:
    """取队首 pending 指令（executor 轮询）。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, action, target FROM executor_commands WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def mark_result(cmd_id: int, ok: bool, result: str) -> None:
    conn = connect()
    try:
        conn.execute(
            """UPDATE executor_commands SET status=?, result=?, executed_at=?
               WHERE id=?""",
            ("done" if ok else "failed", result[:500], _now(), cmd_id),
        )
        conn.commit()
    finally:
        conn.close()


def check_roots(target: str) -> bool:
    """白名单检查：list_dir/read_file 目标须在允许根目录内。未配置=全禁止。"""
    roots = [r.strip() for r in (settings.executor_roots or "").split(",") if r.strip()]
    if not roots:
        return False
    norm = target.replace("\\", "/").lower()
    return any(norm.startswith(r.replace("\\", "/").lower()) for r in roots)
