"""本地执行器：桌面机器人直接执行指令，不经过服务器。

优势（相对服务器队列）：
- 零延迟（无 5s 轮询）
- 隐私：read_file 内容不出本机
- 服务器挂机也能执行本地操作
服务器队列保留（executor_commands）——留给未来 QQ 远程指挥场景。
"""
import ctypes
import os
import re

MAX_LIST = 300  # 单次最多列出的条目数（防病态大目录刷屏）
SENSITIVE_PATTERN = re.compile(
    r"(恢复码|密码|口令|密钥|私钥|token|secret|password|api[_\-]?key)", re.IGNORECASE
)  # 目录名疑似含敏感信息的预警（脱敏原则）


def _natural_key(name: str) -> list:
    """自然排序键：大小写不敏感 + 数字按数值比（f2 < f10）。"""
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", name)]


def _is_hidden_system(path: str) -> bool:
    """Windows 隐藏/系统属性判断（Explorer 默认不显示）；非 Windows 恒为 False。"""
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(path)
        return attrs != 0xFFFFFFFF and bool(attrs & (0x2 | 0x4))  # HIDDEN|SYSTEM
    except Exception:
        return False


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
            if not os.path.isdir(target):
                return False, f"目录不存在：{target}"
            hidden = 0
            entries: list[tuple[str, bool]] = []  # (name, is_dir)
            for name in os.listdir(target):
                full = os.path.join(target, name)
                if _is_hidden_system(full):
                    hidden += 1
                    continue
                entries.append((name, os.path.isdir(full)))
            # 自然排序：大小写不敏感、数字按数值、文件夹在前（Explorer 习惯）
            entries.sort(key=lambda it: _natural_key(it[0]))
            dirs = [n for n, d in entries if d]
            files = [n for n, d in entries if not d]
            total = len(entries)
            lines = [f"- 📁 {n}/" for n, d in entries[:MAX_LIST] if d]
            lines += [f"- 📄 {n}" for n, d in entries[:MAX_LIST] if not d]
            if not lines:
                lines = ["（空目录）"]
            if total > MAX_LIST:
                lines.append(f"… 其余 {total - MAX_LIST} 项")
            sensitive = [n for n, _ in entries if SENSITIVE_PATTERN.search(n)]
            if sensitive:
                names = "、".join(sensitive[:3])
                lines.append(f"⚠️ 发现疑似敏感名称：{names} —— 建议改名或移入 KeePassXC 加密保险库")
            header = f"共 {total} 项（📁 {len(dirs)} 文件夹 / 📄 {len(files)} 文件"
            if hidden:
                header += f"，已隐藏 {hidden} 个系统/隐藏项"
            header += "）："
            return True, header + "\n" + "\n".join(lines)
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
