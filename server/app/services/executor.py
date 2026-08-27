"""执行器（第 11 课）：机器人操作 Windows 的指令队列。

流程：
  聊天命令"帮我打开XX/看看XX目录/读一下XX文件"
  → parse → enqueue（executor_commands 表）
  → Windows executor.py 每 5s 轮询 pending → 执行 → 回传 result
  → 服务器把 result 写为 assistant 消息（下次聊天可见）

安全分级：list_dir/read_file 限制在 EXECUTOR_ALLOWED_ROOTS 白名单内；
open 仅 startfile（打开文件/文件夹/应用，不执行命令）。
"""
import json
import re
from datetime import datetime, timezone

from app.config import settings
from app.models.database import connect


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


STALE_SECONDS = 30 * 60  # pending 指令 30 分钟未被领取 = 过期（防僵尸指令隔天突然执行）


def _pack(a: str, b: str) -> str:
    """双路径操作打包进单 target 字段（表结构不升级）。"""
    return json.dumps([a, b], ensure_ascii=False)


def normalize_target(target: str) -> str:
    """口语盘符规范化：'F盘'→'F:/'、'c盘/xx'→'C:/xx'、'F盘的目录x'→'F:/目录x'。"""
    m = re.match(r"^([A-Za-z])\s*盘[:：]?的?\s*(.*)$", target.strip())
    if m:
        drive = m.group(1).upper()
        rest = m.group(2).strip().lstrip("/\\")
        return f"{drive}:/" + (rest if rest else "")
    return target.strip()


def parse_executor_command(msg: str) -> tuple[str, str] | None:
    """解析操作命令 → (action, target)。

    action: open/list_dir/read_file/copy/backup/move/rename。
    双路径操作 target 为 JSON 数组字符串（见 _pack）。
    注意：run_script 故意不支持——远程跑脚本属安全分级③，只允许桌面本地执行。
    """
    m = re.match(r"^(?:帮我|请)?打开(?:文件夹|目录|应用|软件)?[:：]?\s*(.+)$", msg.strip())
    if m:
        return ("open", normalize_target(m.group(1))[:200])
    m = re.match(r"^(?:列出|看看|查看)(.+?)(?:目录|文件夹)(?:里)?(?:有什么|的内容)?$", msg.strip())
    if m:
        return ("list_dir", normalize_target(m.group(1))[:200])
    m = re.match(r"^(?:帮我|请)?(?:看看|查看|读一下|读取)(?:文件)?[:：]?\s*(.+)$", msg.strip())
    if m and not m.group(1).endswith(("目录", "文件夹")):
        return ("read_file", normalize_target(m.group(1))[:200])
    # ── 第 13 课：文件手（复制/备份/移动/重命名，白名单双路径校验）──
    m = re.match(r"^(?:帮我|请)?(?:复制|拷贝)(.+?)(?:到|至)\s*(.+)$", msg.strip())
    if m:
        return ("copy", _pack(normalize_target(m.group(1))[:200], normalize_target(m.group(2))[:200]))
    m = re.match(r"^(?:帮我|请)?备份(.+?)(?:到|至)\s*(.+)$", msg.strip())
    if m:
        return ("backup", _pack(normalize_target(m.group(1))[:200], normalize_target(m.group(2))[:200]))
    m = re.match(r"^(?:帮我|请)?(?:移动|剪切)(.+?)(?:到|至)\s*(.+)$", msg.strip())
    if m:
        return ("move", _pack(normalize_target(m.group(1))[:200], normalize_target(m.group(2))[:200]))
    m = re.match(r"^(?:帮我|请)?把(.+?)(?:移动到|移到|挪到|挪至)\s*(.+)$", msg.strip())
    if m:
        return ("move", _pack(normalize_target(m.group(1))[:200], normalize_target(m.group(2))[:200]))
    m = re.match(r"^(?:帮我|请)?(?:重命名|改名|把)(.+?)(?:改名为|改为|改成|命名为|叫做|为|成)\s*(.+)$", msg.strip())
    if m:
        return ("rename", _pack(normalize_target(m.group(1))[:200], normalize_target(m.group(2))[:200]))
    return None


def unpack_paths(action: str, target: str) -> list[str]:
    """解出需要白名单校验的全部路径（双路径操作返回两条）。"""
    if action in ("copy", "backup", "move", "rename"):
        try:
            parts = json.loads(target)
            return [str(p) for p in parts]
        except Exception:
            return []
    return [target]


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
    """取队首 pending 指令（executor 轮询）。

    超时未领取的指令自动标记 failed（过期），不再返回——
    否则旧指令会在采集器重启后突然被执行，用户看到"延迟的惊喜"。
    """
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, action, target, created_at FROM executor_commands "
            "WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        created = datetime.fromisoformat(row["created_at"])
        if (datetime.now(timezone.utc) - created).total_seconds() > STALE_SECONDS:
            conn.execute(
                "UPDATE executor_commands SET status='failed', "
                "result='指令已过期（超时未执行）', executed_at=? WHERE id=?",
                (_now(), row["id"]),
            )
            conn.commit()
            return None
        return dict(row)
    finally:
        conn.close()


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
    """白名单检查：list_dir/read_file 目标须在允许根目录内。未配置=全禁止。

    分隔符：分号或逗号均可（.env 建议分号——逗号会被 pydantic-settings 误解析）。
    """
    roots = [
        r.strip()
        for r in settings.executor_allowed_roots.replace(",", ";").split(";")
        if r.strip()
    ]
    if not roots:
        return False
    norm = target.replace("\\", "/").lower()
    return any(norm.startswith(r.replace("\\", "/").lower()) for r in roots)
