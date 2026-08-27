"""本地执行器：桌面机器人直接执行指令，不经过服务器。

优势（相对服务器队列）：
- 零延迟（无 5s 轮询）
- 隐私：read_file 内容不出本机
- 服务器挂机也能执行本地操作
服务器队列保留（executor_commands）——留给未来 QQ 远程指挥场景。
"""
import os
import re


def _get_roots() -> list[str]:
    """实时读白名单（每次执行时读，避免 load_dotenv 时序问题）。"""
    return [
        r.strip().replace("\\", "/").lower()
        for r in os.environ.get("EXECUTOR_ALLOWED_ROOTS", "").replace(",", ";").split(";")
        if r.strip()
    ]


def _normalize(target: str) -> str:
    """口语盘符规范化：'F盘'→'F:/'。"""
    m = re.match(r"^([A-Za-z])\s*盘[:：]?的?\s*(.*)$", target.strip())
    if m:
        drive = m.group(1).upper()
        rest = m.group(2).strip().lstrip("/\\")
        return f"{drive}:/" + (rest if rest else "")
    return target.strip()


def _parse(msg: str) -> tuple[str, str] | None:
    m = re.match(r"^(?:帮我|请)?打开(?:文件夹|目录|应用|软件)?[:：]?\s*(.+)$", msg.strip())
    if m:
        return ("open", _normalize(m.group(1))[:200])
    m = re.match(r"^(?:列出|看看|查看)(.+?)(?:目录|文件夹)(?:里)?(?:有什么|的内容)?$", msg.strip())
    if m:
        return ("list_dir", _normalize(m.group(1))[:200])
    m = re.match(r"^(?:帮我|请)?(?:看看|查看|读一下|读取)(?:文件)?[:：]?\s*(.+)$", msg.strip())
    if m and not m.group(1).endswith(("目录", "文件夹")):
        return ("read_file", _normalize(m.group(1))[:200])
    return None


def _allowed(target: str) -> bool:
    """白名单：list_dir/read_file 须在允许根目录内；未配置=全禁止。"""
    roots = _get_roots()
    if not roots:
        return False
    norm = target.replace("\\", "/").lower()
    return any(norm.startswith(r) for r in roots)


def _execute(action: str, target: str) -> tuple[bool, str]:
    try:
        if action == "open":
            os.startfile(target)
            return True, f"已打开 {target}"
        if action == "list_dir":
            entries = sorted(os.listdir(target))
            dirs = [e for e in entries if os.path.isdir(os.path.join(target, e))]
            files = [e for e in entries if not os.path.isdir(os.path.join(target, e))]
            shown_dirs, shown_files = dirs[:20], files[:20]
            lines = [f"- 📁 {d}/" for d in shown_dirs]
            lines += [f"- 📄 {f}" for f in shown_files]
            shown = len(shown_dirs) + len(shown_files)
            text = "\n".join(lines)
            if len(entries) > shown:
                text += f"\n… 其余 {len(entries) - shown} 项"
            return (
                True,
                f"{target} 共 {len(entries)} 项"
                f"（📁 {len(dirs)} 文件夹 / 📄 {len(files)} 文件）：\n{text}",
            )
        if action == "read_file":
            if not os.path.isfile(target):
                return False, f"文件不存在：{target}"
            with open(target, encoding="utf-8", errors="replace") as f:
                content = f.read(2000)
            return True, content
        return False, f"未知指令类型：{action}"
    except Exception as e:
        return False, f"执行出错：{e}"


def try_execute(msg: str) -> tuple[bool, str]:
    """尝试本地执行；不是执行命令返回 (False, "")。"""
    parsed = _parse(msg)
    if not parsed:
        return (False, "")
    action, target = parsed
    if action in ("list_dir", "read_file"):
        if not _get_roots():
            return (
                True,
                "🔒 未配置白名单：请在项目根 .env 加一行\n"
                "EXECUTOR_ALLOWED_ROOTS=C:/Users/wfy33;F:/\n"
                "（分号分隔多个根目录）",
            )
        if not _allowed(target):
            return (True, f"🔒 该操作超出白名单目录（EXECUTOR_ALLOWED_ROOTS），已拒绝")
    ok, text = _execute(action, target)
    mark = "✅" if ok else "❌"
    return (True, f"{mark} [{action}] {target}\n{text}")
