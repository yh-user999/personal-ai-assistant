"""本地执行器：桌面机器人直接执行指令，不经过服务器。

优势（相对服务器队列）：
- 零延迟（无 5s 轮询）
- 隐私：read_file 内容不出本机
- 服务器挂机也能执行本地操作
服务器队列保留（executor_commands）——留给未来 QQ 远程指挥场景。

第 13 课新增"文件手 + 脚本脚"：
- copy / backup / move / rename：均在白名单根目录内，本地直行
- run_script：仅本地执行器支持（远程 QQ 通道不允许跑脚本——安全分级③）
"""
import ctypes
import os
import re
import shutil
import subprocess
import sys
import time

MAX_LIST = 300  # 单次最多列出的条目数（防病态大目录刷屏）
SCRIPT_TIMEOUT = 120  # 脚本最长运行秒数，超时强制终止
SCRIPT_EXTS = (".py", ".bat", ".cmd")  # 允许的脚本类型（.ps1 暂不支持）
PATH_ACTIONS = ("list_dir", "read_file", "copy", "backup", "move", "rename", "run_script")
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


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


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


def _allowed(target: str) -> bool:
    """白名单：路径类操作须在允许根目录内；未配置=全禁止。"""
    roots = _get_roots()
    if not roots:
        return False
    norm = target.replace("\\", "/").lower()
    return any(norm.startswith(r) for r in roots)


def _resolve_dst(src: str, dst: str, backup: bool = False) -> str:
    """目标路径解析：
    - 目标是目录（已存在或以 / 结尾）→ 放入同名
    - backup=True → 目标下建时间戳子目录（备份语义：永不覆盖）
    """
    if backup:
        sub = "backup-" + time.strftime("%Y%m%d-%H%M%S")
        base = dst if (dst.endswith(("/", "\\")) or os.path.isdir(dst)) else os.path.dirname(dst) or dst
        return os.path.join(base, sub)
    if os.path.isdir(dst) or dst.endswith(("/", "\\")):
        return os.path.join(dst, os.path.basename(src.rstrip("/\\")))
    return dst


def _copy_impl(src: str, dst: str, backup: bool = False) -> tuple[bool, str]:
    if not os.path.exists(src):
        return False, f"源不存在：{src}"
    dst = _resolve_dst(src, dst, backup=backup)
    if os.path.isdir(src):
        existed = os.path.exists(dst)
        shutil.copytree(src, dst, dirs_exist_ok=True)
        note = "（目标已存在，已合并内容）" if existed else ""
        verb = "已备份" if backup else "已复制"
        return True, f"{verb}文件夹：{src} → {dst}{note}"
    if backup:
        # 时间戳目录是文件夹：先建目录，再把文件放进去
        os.makedirs(dst, exist_ok=True)
        dst = os.path.join(dst, os.path.basename(src))
    existed = os.path.isfile(dst)
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    shutil.copy2(src, dst)
    note = "（已覆盖同名文件）" if existed else ""
    verb = "已备份" if backup else "已复制"
    return True, f"{verb}：{src} → {dst}{note}（{_human_size(os.path.getsize(dst))}）"


def _execute(action: str, target: str, extra: str = "") -> tuple[bool, str]:
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
            shown = entries[:MAX_LIST]
            body = [f"共 {total} 项"]
            if hidden:
                body[-1] += f"（已隐藏 {hidden} 个系统/隐藏项）"
            if not dirs and not files:
                body.append("（空目录）")
            else:
                if dirs:
                    body.append("")
                    body.append(f"📁 文件夹（{len(dirs)}）：")
                    body.append("")
                    body.append(" · ".join(f"{n}/" for n, d in shown if d))
                if files:
                    body.append("")
                    body.append(f"📄 文件（{len(files)}）：")
                    body.append("")
                    body.append(" · ".join(n for n, d in shown if not d))
            if total > MAX_LIST:
                body.append("")
                body.append(f"… 其余 {total - MAX_LIST} 项（可进入子目录继续查看）")
            text = "\n".join(body)
            sensitive = [n for n, _ in entries if SENSITIVE_PATTERN.search(n)]
            if sensitive:
                names = "、".join(sensitive[:3])
                text += f"\n⚠️ 发现疑似敏感名称：{names} —— 建议改名或移入 KeePassXC 加密保险库"
            return True, text
        if action == "read_file":
            if not os.path.isfile(target):
                return False, f"文件不存在：{target}"
            with open(target, encoding="utf-8", errors="replace") as f:
                content = f.read(2000)
            return True, content
        # ── 第 13 课：文件手 ────────────────────────────────────
        if action == "copy":
            return _copy_impl(target, extra)
        if action == "backup":
            return _copy_impl(target, extra, backup=True)
        if action == "move":
            if not os.path.exists(target):
                return False, f"源不存在：{target}"
            dst = _resolve_dst(target, extra)
            os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
            try:
                shutil.move(target, dst)
            except (FileExistsError, shutil.Error) as e:
                return False, f"移动失败（目标已存在同名项）：{e}"
            return True, f"已移动：{target} → {dst}"
        if action == "rename":
            if not os.path.exists(target):
                return False, f"源不存在：{target}"
            new = extra
            if not os.path.dirname(new):
                new = os.path.join(os.path.dirname(target) or ".", new)
            try:
                shutil.move(target, new)
            except (FileExistsError, shutil.Error) as e:
                return False, f"重命名失败（目标已存在同名项）：{e}"
            return True, f"已重命名：{target} → {new}"
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
        if not _get_roots():
            return (
                True,
                "🔒 未配置白名单：请在项目根 .env 加一行\n"
                "EXECUTOR_ALLOWED_ROOTS=C:/Users/wfy33;F:/\n"
                "（分号分隔多个根目录）",
            )
        paths = [p for p in (target, extra) if p]
        if not all(_allowed(p) for p in paths):
            return (True, f"🔒 该操作超出白名单目录（EXECUTOR_ALLOWED_ROOTS），已拒绝")
    ok, text = _execute(action, target, extra)
    mark = "✅" if ok else "❌"
    desc = f"{target} → {extra}" if extra else target
    return (True, f"{mark} [{action}] {desc}\n{text}")
