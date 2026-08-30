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
import os
import re
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.models.database import connect

REMOTE_BLOCKED_EXTS = {
    ".bat", ".cmd", ".py", ".pyw", ".ps1", ".js", ".jse", ".vbs", ".vbe",
    ".wsf", ".wsh", ".hta", ".scr", ".jar", ".msi", ".reg",
    ".exe", ".com", ".cpl", ".pif", ".lnk", ".url",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


STALE_SECONDS = 30 * 60  # pending 指令 30 分钟未被领取 = 过期（防僵尸指令隔天突然执行）
CLAIM_SECONDS = 10 * 60  # claimed 10 分钟未回传 = 执行器中途失联，释放为 failed


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


def _looks_like_path(s: str) -> bool:
    """目录参数必须像本地路径（盘符/斜杠/'盘'字），否则不算文件搜索。
    （防止"搜索淘宝里的switch"这类网页搜索句式被误吞）"""
    return bool(re.search(r"[A-Za-z]:|[/\\]|盘", s))


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
    # ── 第 6.24 课：文件搜索（入队给 Windows 执行器，与桌面本地解析同规则）──
    m = re.match(
        r"^(?:帮我|请)?(?:找一下|找找|搜索|查找|找)\s*(.+?)(?:里|中|下|内)的?"
        r"(内容包含|内容含|包含|含|名字里有|文件名带)?\s*(.+?)(?:的)?(?:文件|文档)?[?？!！。]?$",
        msg.strip(),
    )
    if m and m.group(1).strip() and m.group(3).strip() and _looks_like_path(m.group(1)):
        marker = m.group(2) or ""
        kw = ("content:" if marker.startswith("内容") else "") + m.group(3).strip()
        return ("search_files", _pack(normalize_target(m.group(1).strip())[:200], kw[:100]))
    m = re.match(
        r"^(?:帮我|请)?(?:在)?(.+?)(?:里|中|下|内)(?:找一下|找找|搜索|查找|找)"
        r"(内容包含|内容含|包含|含|名字里有|文件名带)?\s*(.+?)(?:的)?(?:文件|文档)?[?？!！。]?$",
        msg.strip(),
    )
    if m and m.group(1).strip() and m.group(3).strip() and _looks_like_path(m.group(1)):
        marker = m.group(2) or ""
        kw = ("content:" if marker.startswith("内容") else "") + m.group(3).strip()
        return ("search_files", _pack(normalize_target(m.group(1).strip())[:200], kw[:100]))
    m = re.match(
        r"^(?:帮我|请)?(?:找一下|找找|搜索|查找|找)"
        r"(内容包含|内容含|包含|含|名字里有|文件名带)?\s*(.+?)(?:的)?(文件|文档)?[?？!！。]?$",
        msg.strip(),
    )
    if m and m.group(2).strip() and (m.group(1) or m.group(3)):
        marker = m.group(1) or ""
        kw = ("content:" if marker.startswith("内容") else "") + m.group(2).strip()
        return ("search_files", _pack("", kw[:100]))
    return None


def unpack_paths(action: str, target: str) -> list[str]:
    """解出需要白名单校验的全部路径（双路径操作返回两条）。"""
    if action in ("copy", "backup", "move", "rename"):
        try:
            parts = json.loads(target)
            return [str(p) for p in parts]
        except Exception:
            return []
    if action == "search_files":
        try:
            parts = json.loads(target)
            dir_spec = str(parts[0])
            return [dir_spec] if dir_spec else []  # 空目录 = 全白名单搜索（执行端逐根校验）
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
    """原子认领队首 pending 指令（executor 轮询）。

    认领即置 status='claimed' 并记录 claimed_at：同一指令不会被两个轮询
    重复领取，也不会因回传丢失被反复执行（旧实现执行期间仍标记 pending，
    采集器回传失败时会每 5s 重跑同一指令直到过期）。超时清理：
    - pending 超 30 分钟未领取 → failed（过期，防"延迟的惊喜"）
    - claimed 超 10 分钟未回传 → failed（执行器失联，不永久占队）
    """
    conn = connect()
    try:
        now = datetime.now(timezone.utc)
        conn.execute(
            "UPDATE executor_commands SET status='failed', "
            "result='指令已过期（超时未执行）', executed_at=? "
            "WHERE status='pending' AND created_at < ?",
            (_now(), (now - timedelta(seconds=STALE_SECONDS)).isoformat()),
        )
        conn.execute(
            "UPDATE executor_commands SET status='failed', "
            "result='执行器回传超时，已标记失败', executed_at=? "
            "WHERE status='claimed' AND claimed_at < ?",
            (_now(), (now - timedelta(seconds=CLAIM_SECONDS)).isoformat()),
        )
        cur = conn.execute(
            "UPDATE executor_commands SET status='claimed', claimed_at=? "
            "WHERE id = (SELECT id FROM executor_commands "
            "            WHERE status='pending' ORDER BY id LIMIT 1) "
            "RETURNING id, action, target, created_at",
            (_now(),),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        conn.close()


def mark_result(cmd_id: int, ok: bool, result: str) -> bool:
    """只接受已认领指令的一次性结果回传，防止伪造任意执行结果。"""
    conn = connect()
    try:
        cur = conn.execute(
            """UPDATE executor_commands SET status=?, result=?, executed_at=?
               WHERE id=? AND status='claimed'""",
            ("done" if ok else "failed", result[:3000], _now(), cmd_id),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _allowed_roots() -> list[str]:
    """解析白名单根目录：realpath + normcase（解析链接，防 junction 绕过）。"""
    return [
        os.path.normcase(os.path.realpath(r.strip().replace("\\", "/")))
        for r in settings.executor_allowed_roots.replace(",", ";").split(";")
        if r.strip()
    ]


def _path_in_roots(target: str, roots: list[str]) -> bool:
    """判断归一化后的 target 是否等于某根目录或位于其内部。

    根目录补尾分隔符再做前缀比较，堵住兄弟目录绕过
    （C:/Users/wfy33-evil 不属于 C:/Users/wfy33）；abspath 已折叠 ../ 穿越。
    """
    norm = os.path.normcase(os.path.realpath(target.replace("\\", "/")))
    return any(norm == root or norm.startswith(root.rstrip("\\/") + os.sep) for root in roots)


def check_open_target(target: str) -> bool:
    """服务端预检 open：拒绝 URL/可执行文件，Windows 路径白名单由采集器本地复核。

    服务端运行在 Linux，不能用本机 abspath 判断 F:/ 等 Windows 路径；
    采集器是实际执行点，必须再调用 path_allowed 做最终校验。
    无路径参数仅作为已登记启动器别名交给采集器。
    """
    text = target.strip()
    if not text or len(text) > 200:
        return False
    if re.match(r"^[a-z][a-z0-9+.-]*://", text, re.IGNORECASE):
        return False
    ext = os.path.splitext(text)[1].casefold()
    if ext in REMOTE_BLOCKED_EXTS:
        return False
    return True


def check_roots(target: str) -> bool:
    """白名单检查：list_dir/read_file 目标须在允许根目录内。未配置=全禁止。

    分隔符：分号或逗号均可（.env 建议分号——逗号会被 pydantic-settings 误解析）。
    """
    roots = _allowed_roots()
    if not roots:
        return False
    return _path_in_roots(target, roots)
