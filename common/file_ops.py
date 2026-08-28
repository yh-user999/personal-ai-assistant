"""执行器共享文件操作：collector 与 desktop 两处执行器的公共实现。

纯逻辑、无网络依赖：白名单校验、目录列表渲染、读文件、
复制/备份/移动/重命名。两处执行器行为保持一致，安全修复只改这一份。
"""
import ctypes
import os
import re
import shutil
import time

MAX_LIST = 300  # 单次最多列出的条目数（防病态大目录刷屏）
# open 永不经 startfile 执行的扩展名（脚本是"打开即执行"的高危类型）
OPEN_BLOCKED_EXTS = (
    ".bat", ".cmd", ".py", ".pyw", ".ps1", ".js", ".jse", ".vbs", ".vbe",
    ".wsf", ".wsh", ".hta", ".scr", ".jar", ".msi", ".reg",
)
SENSITIVE_PATTERN = re.compile(
    r"(恢复码|密码|口令|密钥|私钥|token|secret|password|api[_\-]?key)", re.IGNORECASE
)  # 名称疑似含敏感信息的预警（脱敏原则）


def natural_key(name: str) -> list:
    """自然排序键：大小写不敏感 + 数字按数值比（f2 < f10）。"""
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", name)]


def is_hidden_system(path: str) -> bool:
    """Windows 隐藏/系统属性判断（Explorer 默认不显示）；非 Windows 恒为 False。"""
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(path)
        return attrs != 0xFFFFFFFF and bool(attrs & (0x2 | 0x4))  # HIDDEN|SYSTEM
    except Exception:
        return False


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


# ── 白名单 ────────────────────────────────────────────────

def get_roots(env_value: str | None = None) -> list[str]:
    """白名单根目录，归一化为绝对路径。env_value 缺省读 EXECUTOR_ALLOWED_ROOTS；未配置=全禁止。"""
    raw = os.environ.get("EXECUTOR_ALLOWED_ROOTS", "") if env_value is None else env_value
    return [
        os.path.normcase(os.path.abspath(r.strip().replace("\\", "/")))
        for r in raw.replace(",", ";").split(";")
        if r.strip()
    ]


def path_allowed(target: str, env_value: str | None = None) -> bool:
    """目标须等于某根目录或位于其内部。

    根目录补尾分隔符再做前缀比较，堵住兄弟目录绕过
    （C:/Users/wfy33-evil 不属于 C:/Users/wfy33）；abspath 已折叠 ../ 穿越。
    """
    roots = get_roots(env_value)
    if not roots:
        return False
    norm = os.path.normcase(os.path.abspath(target.replace("\\", "/")))
    return any(norm == root or norm.startswith(root.rstrip("\\/") + os.sep) for root in roots)


# ── 读取类操作 ────────────────────────────────────────────

def list_dir_text(target: str) -> tuple[bool, str]:
    """列目录（自然排序、隐藏系统项过滤、敏感名预警）。"""
    if not os.path.isdir(target):
        return False, f"目录不存在：{target}"
    hidden = 0
    entries: list[tuple[str, bool]] = []  # (name, is_dir)
    for name in os.listdir(target):
        full = os.path.join(target, name)
        if is_hidden_system(full):
            hidden += 1
            continue
        entries.append((name, os.path.isdir(full)))
    # 自然排序：大小写不敏感、数字按数值、文件夹在前（Explorer 习惯）
    entries.sort(key=lambda it: natural_key(it[0]))
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


def read_file_text(target: str, limit: int = 2000) -> tuple[bool, str]:
    """读文件前 limit 字符。"""
    if not os.path.isfile(target):
        return False, f"文件不存在：{target}"
    with open(target, encoding="utf-8", errors="replace") as f:
        content = f.read(limit)
    return True, content


# ── 文件手：copy / backup / move / rename ─────────────────

def resolve_dst(src: str, dst: str, backup: bool = False) -> str:
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


def copy_impl(src: str, dst: str, backup: bool = False) -> tuple[bool, str]:
    if not os.path.exists(src):
        return False, f"源不存在：{src}"
    dst = resolve_dst(src, dst, backup=backup)
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
    size = human_size(os.path.getsize(dst))
    return True, f"{verb}：{src} → {dst}{note}（{size}）"


def move_impl(src: str, dst: str) -> tuple[bool, str]:
    if not os.path.exists(src):
        return False, f"源不存在：{src}"
    dst = resolve_dst(src, dst)
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    try:
        shutil.move(src, dst)
    except (FileExistsError, shutil.Error) as e:
        return False, f"移动失败（目标已存在同名项）：{e}"
    return True, f"已移动：{src} → {dst}"


def rename_impl(src: str, new_name: str) -> tuple[bool, str]:
    if not os.path.exists(src):
        return False, f"源不存在：{src}"
    new = new_name
    if not os.path.dirname(new):
        new = os.path.join(os.path.dirname(src) or ".", new)
    try:
        shutil.move(src, new)
    except (FileExistsError, shutil.Error) as e:
        return False, f"重命名失败（目标已存在同名项）：{e}"
    return True, f"已重命名：{src} → {new}"
