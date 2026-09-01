"""指令确认层：破坏性/低置信度指令先问一句再执行。

为什么需要这层（LESSONS：正则治不了自然语言歧义）：
命令路由靠正则匹配自然语言，"把X改成Y"既可能是重命名文件、也可能是让 AI
改写文本。形态闸门（executor._valid_pair 等）能挡掉大部分误吞，但
"打开新世界的大门"这类 6 字中文短语与真实别名形态完全一致——服务端拿不到
Windows 本机的别名表，无法靠规则区分。

设计取舍：
- 只对**破坏性**（move/rename）与**低置信度**指令要确认；读类操作
  （list_dir/read_file/search_files）和明确路径的 open 不打扰用户。
- 待确认指令存内存不入库：单进程部署，重启丢弃是正确行为——
  重启后用户早已换了话题，"隔天弹出的确认"比丢弃更糟。
- 超时 3 分钟：短于执行器的 30 分钟入队有效期，避免"确认了但指令已过期"。
- 按用户隔离：访客根本用不到执行器，但键上带 uid 防止多人串味。
"""
import time

# 需要确认的动作：会改变磁盘内容且不可逆
DESTRUCTIVE_ACTIONS = frozenset({"move", "rename"})

PENDING_TTL_SECONDS = 180  # 待确认指令存活时长
MAX_PENDING = 200          # 上限，防伪造 uid 撑内存

# uid → {"action", "target", "desc", "ts"}
_pending: dict[str, dict] = {}

CONFIRM_WORDS = frozenset({
    "确认", "确定", "是", "对", "好", "好的", "可以", "行", "嗯", "执行",
    "yes", "y", "ok", "sure", "确认执行",
})
CANCEL_WORDS = frozenset({
    "取消", "不", "不用", "不要", "算了", "别", "停", "no", "n", "cancel",
})


def _prune(now: float) -> None:
    """清理过期项；顺带压到上限内（最老先出）。"""
    for uid in [u for u, v in _pending.items() if now - v["ts"] > PENDING_TTL_SECONDS]:
        _pending.pop(uid, None)
    while len(_pending) > MAX_PENDING:
        _pending.pop(next(iter(_pending)), None)


def needs_confirm(action: str, target: str, confident: bool = True) -> bool:
    """该指令是否要先问一句。

    confident=False 表示解析置信度低（如 open 目标形态像别名但无法核实），
    这类即使不是破坏性动作也要确认——否则用户聊天被当指令执行。
    """
    if action in DESTRUCTIVE_ACTIONS:
        return True
    return not confident


def remember(uid: str, action: str, target: str, desc: str) -> None:
    """记下待确认指令（同一用户只保留最新一条：新指令自然取代旧的）。"""
    now = time.time()
    _pending[uid] = {"action": action, "target": target, "desc": desc, "ts": now}
    # 插入后再裁剪：先裁剪会让上限变成 MAX_PENDING+1
    _prune(now)


def take(uid: str) -> dict | None:
    """取出并清除待确认指令（未过期才返回）。"""
    now = time.time()
    _prune(now)
    item = _pending.pop(uid, None)
    if item is None:
        return None
    if now - item["ts"] > PENDING_TTL_SECONDS:
        return None
    return item


def peek(uid: str) -> dict | None:
    """查看是否有待确认指令，不消费。"""
    _prune(time.time())
    return _pending.get(uid)


def parse_reply(msg: str) -> str | None:
    """把用户回复解析为 'confirm' / 'cancel' / None（都不是）。"""
    text = (msg or "").strip().rstrip("！!。.~ ").casefold()
    if not text:
        return None
    if text in CONFIRM_WORDS:
        return "confirm"
    if text in CANCEL_WORDS:
        return "cancel"
    return None


def clear(uid: str) -> None:
    _pending.pop(uid, None)


def reset() -> None:
    """测试用：清空全部待确认状态。"""
    _pending.clear()
