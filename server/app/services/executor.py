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
import secrets
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


def _exec_ext(target: str) -> str:
    """取用于黑名单比较的扩展名（已归一化，小写）。

    与 common/file_ops.py 的 exec_ext 语义一致——服务端运行在 Linux 且不把
    仓库根放进 sys.path，无法 import common，故此处保留一份等价实现；
    两处任一修改都必须同步（tests/test_executor_security.py 交叉校验）。
    裸 splitext 曾被三类变形绕过：'x.bat.'→'.'、'x.bat '→'.bat '、
    'x.bat::$DATA'→'.bat::$data'，Windows 打开时都照常执行 x.bat。
    """
    text = (target or "").strip()
    stream = text.find(":", 2)
    if stream != -1:
        text = text[:stream]
    return os.path.splitext(text.rstrip(". \t"))[1].casefold()


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


# 已登记别名之外，open 允许的"不像路径"目标：无。open 的目标要么像路径，
# 要么是启动器别名（由执行端 try_launch 解析）——两者都不满足时不该入队。
_ALIAS_RE = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff .+#-]{0,29}$")


def _looks_like_target(s: str) -> bool:
    """像"可执行的目标"：路径，或短别名（≤30 字、无空白断句、无句末语气词）。

    误吞治理的核心闸门。此前 open/read_file/rename/copy/backup 都没有任何
    形态校验，导致"打开心结吧""看看今天天气怎么样""把这段话改成更正式的语气"
    被当成文件操作——10 条正常聊天实测 10 条全中。search_files 早有
    _looks_like_path 护栏，本函数把同类保护补齐到其余动作。
    """
    text = (s or "").strip()
    if not text:
        return False
    if _looks_like_path(text):
        return True
    # 别名形态：不含空格/标点断句，长度受限。"心结吧""今天天气怎么样"这类
    # 自然语言短语虽然也无空格，但由调用方的语气词/长度规则进一步排除。
    return bool(_ALIAS_RE.match(text))


# 句末语气词/疑问词：出现即判定为聊天而非指令（"打开心结吧"的"吧"）
_CHATTY_TAIL = re.compile(r"(吧|吗|呢|啊|呀|么|哦|嘛|没有|如何|怎样|怎么样|好不好|行不行)[?？!！。]?$")
# 明显的疑问/讨论句式：整句出现即不当作文件指令
_CHATTY_ANY = re.compile(r"(怎么|为什么|为何|是否|建议|觉得|可以吗|有什么|哪些|哪个|多少|比较好)")


def _looks_chatty(s: str) -> bool:
    """像自然语言聊天而非指令目标。"""
    text = (s or "").strip()
    return bool(_CHATTY_TAIL.search(text) or _CHATTY_ANY.search(text))


def _valid_single(target: str) -> bool:
    """单路径动作（open/list_dir/read_file）的目标校验。"""
    return _looks_like_target(target) and not _looks_chatty(target)


def _valid_pair(a: str, b: str) -> bool:
    """双路径动作（copy/backup/move/rename）的校验。

    要求：至少一侧像真实路径（含盘符或斜杠），且两侧都不是聊天口气。
    "把这段话改成更正式的语气"两侧都不像路径 → 不再被当成 rename。
    """
    if _looks_chatty(a) or _looks_chatty(b):
        return False
    strong = re.compile(r"[A-Za-z]:[/\\]|[/\\]|盘")
    if not (strong.search(a) or strong.search(b)):
        return False
    return bool(a.strip() and b.strip())


# open 的别名形态上限：已登记别名都很短（"微信""B站""VSCode"）。
# 服务端不持有别名表（launcher.json 在 Windows 本机），只能按形态判断——
# 超过这个长度的中文短语几乎不可能是别名，更像被误吞的聊天内容。
MAX_ALIAS_CHARS = 6


def looks_like_path_target(target: str) -> bool:
    """公开版 _looks_like_path（供 chat 层判断 open 的置信度）。"""
    return _looks_like_path((target or "").strip())


# 真实别名的形态：中文 App 名基本是 2-4 字（微信/钉钉/网易云音乐），
# 英文别名（VSCode/Chrome/B站）含 ASCII 字母。长中文短语才是可疑的误吞。
_SHORT_CJK_ALIAS = 4


def confident_open_target(target: str) -> bool:
    """open 目标是否高置信（无需确认即可入队）。

    高置信：给了明确路径，或形态就是常见别名（短中文名/含 ASCII 的名字）。
    低置信：5 字以上的纯中文短语——与"打开新世界的大门"这类误吞无法区分，
    交给确认层问一句。桌面端不受影响：local_exec 会先本地拦截并直接执行。
    """
    text = (target or "").strip()
    if not text:
        return False
    if _looks_like_path(text):
        return True
    if re.search(r"[A-Za-z0-9]", text):
        return True  # VSCode / Chrome / B站 等含 ASCII 的别名
    return len(text) <= _SHORT_CJK_ALIAS


_ACTION_VERBS = {
    "open": "打开",
    "list_dir": "列出目录",
    "read_file": "读取",
    "copy": "复制",
    "backup": "备份",
    "move": "移动",
    "rename": "重命名",
    "search_files": "搜索",
}


def describe_command(action: str, target: str) -> str:
    """把指令描述成一句人话（确认提示用）。"""
    verb = _ACTION_VERBS.get(action, action)
    paths = unpack_paths(action, target)
    if action in ("copy", "backup", "move", "rename") and len(paths) == 2:
        return f"把 {paths[0]} {verb}到 {paths[1]}"
    return f"{verb} {paths[0] if paths else target}"


def plausible_open_target(target: str) -> bool:
    """open 目标是否值得入队（形态上像路径或像别名）。

    背景：服务端拿不到 Windows 本机的别名表，无法确认"思路想想别的办法"是不是
    用户注册过的别名。此前一律入队，执行端拒绝后用户收到安全提示而非回答。
    改为：像路径 → 入队；短到像别名 → 入队（真别名走这条）；
    长中文短语 → 不入队，落回 LLM 当聊天处理。
    """
    text = (target or "").strip()
    if not text:
        return False
    if _looks_like_path(text):
        return True
    # 含空白的多词短语不是别名
    if re.search(r"\s", text):
        return False
    return len(text) <= MAX_ALIAS_CHARS


def parse_executor_command(msg: str) -> tuple[str, str] | None:
    """解析操作命令 → (action, target)。

    action: open/list_dir/read_file/copy/backup/move/rename。
    双路径操作 target 为 JSON 数组字符串（见 _pack）。
    注意：run_script 故意不支持——远程跑脚本属安全分级③，只允许桌面本地执行。
    """
    m = re.match(r"^(?:帮我|请)?打开(?:文件夹|目录|应用|软件)?[:：]?\s*(.+)$", msg.strip())
    if m:
        target = normalize_target(m.group(1))[:200]
        if _valid_single(target):
            return ("open", target)
    m = re.match(r"^(?:列出|看看|查看)(.+?)(?:目录|文件夹)(?:里)?(?:有什么|的内容)?$", msg.strip())
    if m:
        target = normalize_target(m.group(1))[:200]
        if _valid_single(target):
            return ("list_dir", target)
    m = re.match(r"^(?:帮我|请)?(?:看看|查看|读一下|读取)(?:文件)?[:：]?\s*(.+)$", msg.strip())
    if m and not m.group(1).endswith(("目录", "文件夹")):
        target = normalize_target(m.group(1))[:200]
        # read_file 要求真实路径特征：单纯别名（"今天天气怎么样"）不该读文件
        if _looks_like_path(target) and not _looks_chatty(target):
            return ("read_file", target)
    # ── 第 13 课：文件手（复制/备份/移动/重命名，白名单双路径校验）──
    m = re.match(r"^(?:帮我|请)?(?:复制|拷贝)(.+?)(?:到|至)\s*(.+)$", msg.strip())
    if m:
        a, b = normalize_target(m.group(1))[:200], normalize_target(m.group(2))[:200]
        if _valid_pair(a, b):
            return ("copy", _pack(a, b))
    m = re.match(r"^(?:帮我|请)?备份(.+?)(?:到|至)\s*(.+)$", msg.strip())
    if m:
        a, b = normalize_target(m.group(1))[:200], normalize_target(m.group(2))[:200]
        if _valid_pair(a, b):
            return ("backup", _pack(a, b))
    m = re.match(r"^(?:帮我|请)?(?:移动|剪切)(.+?)(?:到|至)\s*(.+)$", msg.strip())
    if m:
        a, b = normalize_target(m.group(1))[:200], normalize_target(m.group(2))[:200]
        if _valid_pair(a, b):
            return ("move", _pack(a, b))
    m = re.match(r"^(?:帮我|请)?把(.+?)(?:移动到|移到|挪到|挪至)\s*(.+)$", msg.strip())
    if m:
        a, b = normalize_target(m.group(1))[:200], normalize_target(m.group(2))[:200]
        if _valid_pair(a, b):
            return ("move", _pack(a, b))
    m = re.match(r"^(?:帮我|请)?(?:重命名|改名|把)(.+?)(?:改名为|改为|改成|命名为|叫做|为|成)\s*(.+)$", msg.strip())
    if m:
        a, b = normalize_target(m.group(1))[:200], normalize_target(m.group(2))[:200]
        if _valid_pair(a, b):
            return ("rename", _pack(a, b))
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


def get_pending(device_id: str = "") -> dict | None:
    """原子认领队首 pending 指令并签发持久 lease。"""
    conn = connect()
    try:
        now = datetime.now(timezone.utc)
        now_s = now.isoformat()
        conn.execute(
            "UPDATE executor_commands SET status='failed', result='指令已过期（超时未执行）', executed_at=? "
            "WHERE status='pending' AND created_at < ?",
            (_now(), (now - timedelta(seconds=STALE_SECONDS)).isoformat()),
        )
        # 租约过期转 unknown：允许带原 token 的迟到结果完成，避免执行器实际成功却丢失。
        conn.execute(
            "UPDATE executor_commands SET status='unknown', result='执行器租约已过期，等待迟到结果', result_late=1 "
            "WHERE status='claimed' AND COALESCE(lease_expires_at, claimed_at) < ?",
            (now_s,),
        )
        token = secrets.token_urlsafe(24)
        lease = (now + timedelta(seconds=CLAIM_SECONDS)).isoformat()
        cur = conn.execute(
            "UPDATE executor_commands SET status='claimed', claimed_at=?, device_id=?, claim_token=?, lease_expires_at=? "
            "WHERE id = (SELECT id FROM executor_commands WHERE status='pending' ORDER BY id LIMIT 1) "
            "RETURNING id, action, target, created_at, device_id, claim_token, lease_expires_at",
            (now_s, device_id[:100], token, lease),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        conn.close()


def mark_result(cmd_id: int, ok: bool, result: str, claim_token: str = "", device_id: str = "") -> bool:
    """接受匹配 token 的结果；租约过期的 unknown 结果标记为迟到但仍落库。"""
    conn = connect()
    try:
        where = "id=? AND status IN ('claimed','unknown')"
        params: list = ["done" if ok else "failed", result[:3000], _now(), cmd_id]
        if claim_token:
            where += " AND claim_token=?"
            params.append(claim_token)
        if device_id:
            where += " AND device_id=?"
            params.append(device_id[:100])
        cur = conn.execute(
            f"UPDATE executor_commands SET status=?, result=?, executed_at=?, result_late=CASE WHEN status='unknown' THEN 1 ELSE result_late END WHERE {where}",
            params,
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
    # 盘符之后再出现冒号 = NTFS 备用数据流（x.bat::$DATA 仍会执行 x.bat），
    # 正常业务路径不会用到，出现即视为绕过尝试。
    if text.find(":", 2) != -1:
        return False
    if _exec_ext(text) in REMOTE_BLOCKED_EXTS:
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
