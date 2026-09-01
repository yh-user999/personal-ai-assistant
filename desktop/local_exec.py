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

from common import launcher
from common.file_ops import (
    copy_impl,
    exec_ext,
    get_roots,
    is_blocked_open,
    list_dir_text,
    move_impl,
    path_allowed,
    read_file_text,
    rename_impl,
    search_files_impl,
)

SCRIPT_TIMEOUT = 120  # 脚本最长运行秒数，超时强制终止
SCRIPT_EXTS = (".py", ".bat", ".cmd")  # 允许的脚本类型（.ps1 暂不支持）
PATH_ACTIONS = ("list_dir", "read_file", "copy", "backup", "move", "rename", "run_script", "find_files")

# ── 快捷启动器（第 14 课）语法 ─────────────────────────────
_LAUNCHER_ADD_RE = re.compile(r"^(?:帮我|请)?记住\s*(.+?)\s*[=＝]\s*(.+)$")
_ADD_BROWSER_RE = re.compile(r"^用(\S+?)打开(.+)$")
_ADD_SEARCH_RE = re.compile(r"^(?:在|去)(.+?)搜索$")
_ADD_OPEN_RE = re.compile(r"^打开(.+)$")
_BROWSER_OPEN_RE = re.compile(r"^(?:帮我|请)?用(\S+?)打开(.+)$")
_SEARCH_RE = re.compile(r"^(?:帮我|请)?(?:在|去)(.+?)(?:上|里)?搜索(.+)$")
_FORGET_RE = re.compile(r"^(?:帮我|请)?(?:忘掉|删掉|删除)(?:常用|快捷方式)?\s*(.+)$")
_LIST_RE = re.compile(r"^(?:帮我|请)?(?:看看)?我的常用(?:列表|软件|网站|网址|收藏)?[?？!！]?$")
# 时段推荐：把 use_count 的时段分桶用起来（"现在该开什么"）
_SUGGEST_RE = re.compile(
    r"^(?:帮我|请)?(?:现在|这会儿|这个点)?(?:我)?(?:常用推荐|推荐常用|该开什么|开什么好|"
    r"平时用什么|这个点用什么)[?？!！]?$"
)


def _normalize(target: str) -> str:
    """口语盘符规范化：'F盘'→'F:/'。"""
    m = re.match(r"^([A-Za-z])\s*盘[:：]?的?\s*(.*)$", target.strip())
    if m:
        drive = m.group(1).upper()
        rest = m.group(2).strip().lstrip("/\\")
        return f"{drive}:/" + (rest if rest else "")
    return target.strip()


def _looks_like_path(s: str) -> bool:
    """目录参数必须像本地路径（盘符/斜杠/‘盘’字），否则不算文件搜索。
    （防止"搜索淘宝里的switch"这种网页搜索句式被误吞成文件搜索）"""
    return bool(re.search(r"[A-Za-z]:|[/\\]|盘", s))


# ── 误吞闸门（与 server/app/services/executor.py 同规则）──────
# 此前只有 find_files 有 _looks_like_path 护栏，open/read_file/rename/copy
# 都没有，导致"把这段话改成更正式的语气"被当成 rename 真的去改文件名。
# 本地执行比远程更危险：没有服务端预检，解析完直接落到 shutil.move。

_ALIAS_RE = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff .+#-]{0,29}$")
_CHATTY_TAIL = re.compile(r"(吧|吗|呢|啊|呀|么|哦|嘛|没有|如何|怎样|怎么样|好不好|行不行)[?？!！。]?$")
_CHATTY_ANY = re.compile(r"(怎么|为什么|为何|是否|建议|觉得|可以吗|有什么|哪些|哪个|多少|比较好)")
_STRONG_PATH = re.compile(r"[A-Za-z]:[/\\]|[/\\]|盘")


def _looks_chatty(s: str) -> bool:
    text = (s or "").strip()
    return bool(_CHATTY_TAIL.search(text) or _CHATTY_ANY.search(text))


def _valid_single(target: str) -> bool:
    """单路径动作（open/list_dir）目标校验：像路径或像短别名，且非聊天口气。"""
    text = (target or "").strip()
    if not text or _looks_chatty(text):
        return False
    return _looks_like_path(text) or bool(_ALIAS_RE.match(text))


def _valid_pair(a: str, b: str) -> bool:
    """双路径动作校验：至少一侧含真实路径特征，两侧都不是聊天口气。"""
    if _looks_chatty(a) or _looks_chatty(b):
        return False
    if not (_STRONG_PATH.search(a) or _STRONG_PATH.search(b)):
        return False
    return bool(a.strip() and b.strip())


def _parse(msg: str) -> tuple[str, str, str] | None:
    """解析操作命令 → (action, 主路径, 副路径)。副路径为空串表示单路径操作。"""
    m = re.match(r"^(?:帮我|请)?打开(?:文件夹|目录|应用|软件)?[:：]?\s*(.+)$", msg.strip())
    if m:
        target = _normalize(m.group(1))[:200]
        if _valid_single(target):
            return ("open", target, "")
    m = re.match(r"^(?:列出|看看|查看)(.+?)(?:目录|文件夹)(?:里)?(?:有什么|的内容)?$", msg.strip())
    if m:
        target = _normalize(m.group(1))[:200]
        if _valid_single(target):
            return ("list_dir", target, "")
    m = re.match(r"^(?:帮我|请)?(?:看看|查看|读一下|读取)(?:文件)?[:：]?\s*(.+)$", msg.strip())
    if m and not m.group(1).endswith(("目录", "文件夹")):
        target = _normalize(m.group(1))[:200]
        # read_file 要求真实路径特征：别名形态（"今天天气怎么样"）不该去读文件
        if _looks_like_path(target) and not _looks_chatty(target):
            return ("read_file", target, "")
    # ── 第 13 课：文件手（复制/备份/移动/重命名）────────────────
    m = re.match(r"^(?:帮我|请)?(?:复制|拷贝)(.+?)(?:到|至)\s*(.+)$", msg.strip())
    if m:
        a, b = _normalize(m.group(1))[:200], _normalize(m.group(2))[:200]
        if _valid_pair(a, b):
            return ("copy", a, b)
    m = re.match(r"^(?:帮我|请)?备份(.+?)(?:到|至)\s*(.+)$", msg.strip())
    if m:
        a, b = _normalize(m.group(1))[:200], _normalize(m.group(2))[:200]
        if _valid_pair(a, b):
            return ("backup", a, b)
    m = re.match(r"^(?:帮我|请)?(?:移动|剪切)(.+?)(?:到|至)\s*(.+)$", msg.strip())
    if m:
        a, b = _normalize(m.group(1))[:200], _normalize(m.group(2))[:200]
        if _valid_pair(a, b):
            return ("move", a, b)
    m = re.match(r"^(?:帮我|请)?把(.+?)(?:移动到|移到|挪到|挪至)\s*(.+)$", msg.strip())
    if m:
        a, b = _normalize(m.group(1))[:200], _normalize(m.group(2))[:200]
        if _valid_pair(a, b):
            return ("move", a, b)
    m = re.match(r"^(?:帮我|请)?(?:重命名|改名|把)(.+?)(?:改名为|改为|改成|命名为|叫做|为|成)\s*(.+)$", msg.strip())
    if m:
        a, b = _normalize(m.group(1))[:200], _normalize(m.group(2))[:200]
        if _valid_pair(a, b):
            return ("rename", a, b)
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
    # ── 第 6.24 课：文件搜索（白名单内按名/内容找文件）────────
    # 句式 A：找 + 目录 + 里 + 关键词（"找F:/projects里的todo文件"）
    m = re.match(
        r"^(?:帮我|请)?(?:找一下|找找|搜索|查找|找)\s*(.+?)(?:里|中|下|内)的?"
        r"(内容包含|内容含|包含|含|名字里有|文件名带)?\s*(.+?)(?:的)?(?:文件|文档)?[?？!！。]?$",
        msg.strip(),
    )
    if m and m.group(1).strip() and m.group(3).strip() and _looks_like_path(m.group(1)):
        marker = m.group(2) or ""
        kw = ("content:" if marker.startswith("内容") else "") + m.group(3).strip()
        return ("find_files", _normalize(m.group(1).strip())[:200], kw[:100])
    # 句式 B：目录 + 里 + 找 + 关键词（"在F盘里找包含todo的文件"）
    m = re.match(
        r"^(?:帮我|请)?(?:在)?(.+?)(?:里|中|下|内)(?:找一下|找找|搜索|查找|找)"
        r"(内容包含|内容含|包含|含|名字里有|文件名带)?\s*(.+?)(?:的)?(?:文件|文档)?[?？!！。]?$",
        msg.strip(),
    )
    if m and m.group(1).strip() and m.group(3).strip() and _looks_like_path(m.group(1)):
        marker = m.group(2) or ""
        kw = ("content:" if marker.startswith("内容") else "") + m.group(3).strip()
        return ("find_files", _normalize(m.group(1).strip())[:200], kw[:100])
    # 句式 C：全白名单搜索（"帮我找包含todo的文件"）
    # 保险：必须有"包含/名字里"标记或"文件/文档"后缀才算文件搜索，
    # 否则"搜索一下北京天气"这类会被误吞
    m = re.match(
        r"^(?:帮我|请)?(?:找一下|找找|搜索|查找|找)"
        r"(内容包含|内容含|包含|含|名字里有|文件名带)?\s*(.+?)(?:的)?(文件|文档)?[?？!！。]?$",
        msg.strip(),
    )
    if m and m.group(2).strip() and (m.group(1) or m.group(3)):
        marker = m.group(1) or ""
        kw = ("content:" if marker.startswith("内容") else "") + m.group(2).strip()
        return ("find_files", "", kw[:100])
    return None


def _execute(action: str, target: str, extra: str = "") -> tuple[bool, str]:
    try:
        if action == "open":
            # 扩展名判定走 file_ops.is_blocked_open（已归一化尾点/尾空格/NTFS 数据流），
            # 三处执行器共用同一判据——本地曾用裸 splitext，'x.bat.' 可绕过。
            if is_blocked_open(target):
                return False, (
                    f"出于安全考虑，不允许打开可执行/快捷方式类型：{exec_ext(target) or target}"
                )
            if re.match(r"^[a-z][a-z0-9+.-]*://", target, re.IGNORECASE):
                return False, "出于安全考虑，未登记的外部 URL 不允许打开"
            if not path_allowed(target):
                return False, "🔒 打开目标超出白名单（EXECUTOR_ALLOWED_ROOTS），本地已拒绝"
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
        # ── 第 6.24 课：文件搜索（公共实现，与采集器共用护栏）──
        if action == "find_files":
            return search_files_impl(target, extra)
        return False, f"未知指令类型：{action}"
    except Exception as e:
        return False, f"执行出错：{e}"


# ── 快捷启动器（第 14 课）────────────────────────────────

def _parse_launcher(msg: str) -> tuple | None:
    """解析启动器管理命令 → ("add", alias, fields) / ("remove", alias) / ("list",)。"""
    msg = msg.strip()
    m = _LAUNCHER_ADD_RE.match(msg)
    if m:
        left, right = m.group(1).strip(), m.group(2).strip()
        browser = ""
        bm = _ADD_BROWSER_RE.match(left)
        if bm:
            browser, left = bm.group(1).casefold(), bm.group(2).strip()
        alias = left
        tm = _ADD_SEARCH_RE.match(left)
        if tm:
            alias = tm.group(1).strip()
            return ("add", alias, {"template": right, "browser": browser})
        om = _ADD_OPEN_RE.match(left)
        if om:
            alias = om.group(1).strip()
        if right.startswith(("http://", "https://")) or re.match(r"^[\w-]+(\.[\w-]+)+(/[^\s]*)?$", right):
            return ("add", alias, {"url": right, "browser": browser})
        if right.startswith(("ms-settings:", "shell:")):
            return ("add", alias, {"shell": right})
        return ("add", alias, {"app": right, "browser": browser})
    if _SUGGEST_RE.match(msg):
        return ("suggest",)
    m = _FORGET_RE.match(msg)
    if m:
        return ("remove", m.group(1).strip())
    if _LIST_RE.match(msg):
        return ("list",)
    return None


def _launcher_command(cmd: tuple) -> str:
    """执行启动器管理命令，返回给用户的回复文本。"""
    if cmd[0] == "add":
        _, alias, fields = cmd
        ok, text = launcher.add_item(alias, **fields)
        if ok:
            return (
                f"✅ 已记住：{text}\n"
                f"（说「打开{alias}」试试；搜索模板说「在{alias}搜索 关键词」）"
            )
        return f"❌ {text}"
    if cmd[0] == "remove":
        ok, text = launcher.remove_item(cmd[1])
        return ("🗑️ 已忘掉：" if ok else "🤔 ") + text
    if cmd[0] == "suggest":
        return _format_suggestions()
    text = launcher.format_list()
    # 列表末尾附上时段推荐：use_count 的时段分桶此前只存不用
    hint = _format_suggestions(inline=True)
    return f"{text}\n{hint}" if hint else text


def _format_suggestions(inline: bool = False) -> str:
    """当前时段的常用推荐。"""
    import datetime

    items = launcher.suggest_for_hour(limit=3)
    if not items:
        return "" if inline else "还没有足够的使用记录，用几次「打开X」我就能猜你想开什么了。"
    hour = datetime.datetime.now().hour
    names = "、".join(it["alias"] for it in items)
    if inline:
        return f"💡 这个点你常用：{names}"
    return f"💡 {hour} 点前后你通常会开：{names}\n（直接说「打开{items[0]['alias']}」）"


def try_execute(msg: str) -> tuple[bool, str]:
    """尝试本地执行；不是执行命令返回 (False, "")。"""
    # ── 快捷启动器：管理命令 + 专用语法（先于通用解析）──
    lc = _parse_launcher(msg)
    if lc:
        return (True, _launcher_command(lc))

    bm = _BROWSER_OPEN_RE.match(msg.strip())
    if bm:
        browser, target = bm.group(1), bm.group(2).strip()
        if browser.casefold() in launcher.load()["browsers"]:
            handled, ok, text = launcher.try_launch(target, browser)
            mark = "✅" if ok else "❌"
            return (True, f"{mark} [open/{browser}] {target}\n{text}")

    sm = _SEARCH_RE.match(msg.strip())
    if sm:
        item = launcher.find_item(sm.group(1).strip(), want="template")
        if item is not None:
            url = launcher.expand_template(item["template"], sm.group(2).strip())
            try:
                launcher._open_url(url, item.get("browser", ""))
                launcher.bump(item["alias"])
                return (True, f"🔍 已打开搜索：{url}")
            except Exception as e:
                return (True, f"❌ 搜索打开失败：{e}")

    parsed = _parse(msg)
    if not parsed:
        return (False, "")
    action, target, extra = parsed
    if action == "open":
        # 先查快捷启动器（注册过的别名优先，可指向白名单外路径）
        handled, ok, text = launcher.try_launch(target)
        if handled:
            mark = "✅" if ok else "❌"
            return (True, f"{mark} [open] {target}\n{text}")
        ok, text = _execute(action, target)
        if not ok:
            near = launcher.find_suggestions(target)
            if near:
                text += f"\n💡 你是想打开：{'、'.join(near)} 吗？（说「打开{near[0]}」）"
        mark = "✅" if ok else "❌"
        return (True, f"{mark} [{action}] {target}\n{text}")
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
        if action == "find_files":
            # extra 是搜索关键词不是路径；只校验目录（空=全白名单搜索）
            if target and not path_allowed(target):
                return (True, "🔒 该操作超出白名单目录（EXECUTOR_ALLOWED_ROOTS），已拒绝")
        else:
            paths = [p for p in (target, extra) if p]
            if not all(path_allowed(p) for p in paths):
                return (True, "🔒 该操作超出白名单目录（EXECUTOR_ALLOWED_ROOTS），已拒绝")
    ok, text = _execute(action, target, extra)
    mark = "✅" if ok else "❌"
    if action == "find_files" and not target:
        desc = f"在所有白名单目录里找「{extra.replace('content:', '内容包含', 1)}」"
    else:
        desc = f"{target} → {extra}" if extra else target
    return (True, f"{mark} [{action}] {desc}\n{text}")
