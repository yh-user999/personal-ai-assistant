"""本地执行器：桌面机器人直接执行指令，不经过服务器。

优势（相对服务器队列）：
- 零延迟（无 5s 轮询）
- 隐私：read_file 内容不出本机
- 服务器挂机也能执行本地操作
服务器队列保留（executor_commands）——留给未来 QQ 远程指挥场景。

第 13 课新增"文件手 + 脚本脚"：
- copy / backup / move / rename：均在白名单根目录内，本地直行
- run_script：仅本地执行器支持（远程 QQ 通道不允许跑脚本——安全分级③）

文件操作的公共实现在 common/file_ops.py（与采集器执行器共用）。
"""
import os
import re
import subprocess
import sys
import time

from common.file_ops import (
    OPEN_BLOCKED_EXTS,
    copy_impl,
    get_roots,
    list_dir_text,
    move_impl,
    path_allowed,
    read_file_text,
    rename_impl,
)

SCRIPT_TIMEOUT = 120  # 脚本最长运行秒数，超时强制终止
SCRIPT_EXTS = (".py", ".bat", ".cmd")  # 允许的脚本类型（.ps1 暂不支持）
PATH_ACTIONS = ("list_dir", "read_file", "copy", "backup", "move", "rename", "run_script")


def _normalize(target: str) -> str:
    """口语盘符规范化：'F盘'→'F:/'。"""
    m = re.match(r"^([A-Za-z])\s*盘[:：]?的?\s*(.*)$", target.strip())
    if m:
        drive = m.group(1).upper()
        rest = m.group(2).strip().lstrip("/\\")
        return f"{drive}:/" + (rest if rest else "")
    return target.strip()


def _parse(msg: str) -> tuple[str, str, str] | None:
    """解析操作命令 → (action, 主路径, 副路径)。副路径为空串表示单路径操作。"""
    m = re.match(r"^(?:帮我|请)?打开(?:文件夹|目录|应用|软件)?[:：]?\s*(.+)$", msg.strip())
    if m:
        return ("open", _normalize(m.group(1))[:200], "")
    m = re.match(r"^(?:列出|看看|查看)(.+?)(?:目录|文件夹)(?:里)?(?:有什么|的内容)?$", msg.strip())
    if m:
        return ("list_dir", _normalize(m.group(1))[:200], "")
    m = re.match(r"^(?:帮我|请)?(?:看看|查看|读一下|读取)(?:文件)?[:：]?\s*(.+)$", msg.strip())
    if m and not m.group(1).endswith(("目录", "文件夹")):
        return ("read_file", _normalize(m.group(1))[:200], "")
    # ── 第 13 课：文件手（复制/备份/移动/重命名）────────────────
    m = re.match(r"^(?:帮我|请)?(?:复制|拷贝)(.+?)(?:到|至)\s*(.+)$", msg.strip())
    if m:
        return ("copy", _normalize(m.group(1))[:200], _normalize(m.group(2))[:200])
    m = re.match(r"^(?:帮我|请)?备份(.+?)(?:到|至)\s*(.+)$", msg.strip())
    if m:
        return ("backup", _normalize(m.group(1))[:200], _normalize(m.group(2))[:200])
    m = re.match(r"^(?:帮我|请)?(?:移动|剪切)(.+?)(?:到|至)\s*(.+)$", msg.strip())
    if m:
        return ("move", _normalize(m.group(1))[:200], _normalize(m.group(2))[:200])
    m = re.match(r"^(?:帮我|请)?把(.+?)(?:移动到|移到|挪到|挪至)\s*(.+)$", msg.strip())
    if m:
        return ("move", _normalize(m.group(1))[:200], _normalize(m.group(2))[:200])
    m = re.match(r"^(?:帮我|请)?(?:重命名|改名|把)(.+?)(?:改名为|改为|改成|命名为|叫做|为|成)\s*(.+)$", msg.strip())
    if m:
        return ("rename", _normalize(m.group(1))[:200], _normalize(m.group(2))[:200])
    # ── 第 13 课：脚本脚（仅本地执行器支持）────────────────────
    m = re.match(
        r"^(?:帮我|请)?(?:运行|跑一下|跑|执行)(?:这个)?(?:脚本|程序)?[:：]?\s*(.+?)(?:脚本|程序)?$",
        msg.strip(),
    )
    if m:
        target = _normalize(m.group(1))[:200]
        # 目标必须"像路径"（盘符/斜杠/脚本扩展名），避免误吞"帮我跑脚本"这类闲聊
        if re.search(r"^[A-Za-z]:/|/|[\\]|\.(?:py|bat|cmd)$", target):
            return ("run_script", target, "")
    return None


def _execute(action: str, target: str, extra: str = "") -> tuple[bool, str]:
    try:
        if action == "open":
            ext = os.path.splitext(target)[1].lower()
            if ext in OPEN_BLOCKED_EXTS:
                return False, f"出于安全考虑，不允许打开脚本/安装包类型：{ext}"
            os.startfile(target)
            return True, f"已打开 {target}"
        if action == "list_dir":
            return list_dir_text(target)
        if action == "read_file":
            return read_file_text(target)
        # ── 第 13 课：文件手 ────────────────────────────────────
        if action == "copy":
            return copy_impl(target, extra)
        if action == "backup":
            return copy_impl(target, extra, backup=True)
        if action == "move":
            return move_impl(target, extra)
        if action == "rename":
            return rename_impl(target, extra)
        # ── 第 13 课：脚本脚（仅本地执行器）──────────────────────
        if action == "run_script":
            if not os.path.isfile(target):
                return False, f"脚本不存在：{target}"
            ext = os.path.splitext(target)[1].lower()
            if ext not in SCRIPT_EXTS:
                return False, f"仅支持脚本类型：{', '.join(SCRIPT_EXTS)}（.ps1 暂不支持）"
            cmd = [sys.executable, target] if ext == ".py" else ["cmd", "/c", target]
            kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW
            t0 = time.time()
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=SCRIPT_TIMEOUT,
                    **kwargs,
                )
            except subprocess.TimeoutExpired:
                return False, f"脚本超时（>{SCRIPT_TIMEOUT}s），已强制终止"
            dt = time.time() - t0
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()[:1500] or "（无输出）"
            if proc.returncode == 0:
                return True, f"脚本执行完成（exit 0，用时 {dt:.1f}s）：\n{out}"
            return False, f"脚本执行失败（exit {proc.returncode}，用时 {dt:.1f}s）：\n{out}"
        return False, f"未知指令类型：{action}"
    except Exception as e:
        return False, f"执行出错：{e}"


def try_execute(msg: str) -> tuple[bool, str]:
    """尝试本地执行；不是执行命令返回 (False, "")。"""
    parsed = _parse(msg)
    if not parsed:
        return (False, "")
    action, target, extra = parsed
    if action in PATH_ACTIONS:
        if not get_roots():
            return (
                True,
                (
                    "🔒 未配置白名单：请在项目根 .env 加一行\n"
                    "EXECUTOR_ALLOWED_ROOTS=C:/Users/wfy33;F:/\n"
                    "（分号分隔多个根目录）"
                ),
            )
        paths = [p for p in (target, extra) if p]
        if not all(path_allowed(p) for p in paths):
            return (True, "🔒 该操作超出白名单目录（EXECUTOR_ALLOWED_ROOTS），已拒绝")
    ok, text = _execute(action, target, extra)
    mark = "✅" if ok else "❌"
    desc = f"{target} → {extra}" if extra else target
    return (True, f"{mark} [{action}] {desc}\n{text}")
