"""身份守卫：改名/改设定要先确认，角色扮演类不进长期人格。

为什么需要这层：`kind=identity` 的教训**永久最高优先注入且不占普通配额**
（见 self_reflect.get_lessons_injection）——这是为了保住"你就叫小月"这条
人格锚点。但同一条通道也会静默吞下别的东西，实测：
- "以后你就是我的猫娘" → identity，永久注入
- "叫你笨蛋好了"       → identity，永久注入
一句玩笑话就能永久改掉她的身份，而用户完全不知道发生了什么。

两道闸门：
1. 角色扮演/身份污染类（猫娘/主人/契约/老公…）一律拒绝入库——这类不是
   "用户的长期偏好"，是一次性的角色要求，不该进人格层。
2. 真正的改名（她已经有名字了，用户要换一个）必须显式确认；
   首次命名不打扰（用户在建立设定，不是在改）。

内存态待确认，与 confirm.py 分开：那边存的是执行器 action/target，
语义不同，混用会让两个模块互相污染。
"""
import re
import time

# 角色扮演 / 身份污染：直接拒绝，不入 lessons
# 这些是"扮演请求"而非"长期偏好"，进了 identity 段会永久扭曲她的人格
ROLEPLAY_PATTERN = re.compile(
    r"猫娘|女仆|妹妹|姐姐|老婆|老公|女friend|男friend|女朋友|男朋友|"
    r"主人|奴|契约|宠物|狗|舔|发情|扮演|角色扮演|roleplay|rp模式|"
    r"人格覆盖|忘记你是|你不是ai|你不再是"
)

# 侮辱性命名：同样不该进人格锚点
INSULT_PATTERN = re.compile(r"笨蛋|蠢货|白痴|傻[逼比子瓜]|废物|垃圾|贱")

# 命名意图（比 self_reflect.IDENTITY_PATTERN 更严：要求出现"名字/叫"语义）
RENAME_PATTERN = re.compile(
    r"(?:你就叫|你叫|就叫你|叫你|给你起名|起名叫|名字叫|你的名字|改名|换个名字)"
)

PENDING_TTL_SECONDS = 180
MAX_PENDING = 100

# uid → {"content": 原话, "ts": 时间}
_pending: dict[str, dict] = {}


def _prune(now: float) -> None:
    for uid in [u for u, v in _pending.items() if now - v["ts"] > PENDING_TTL_SECONDS]:
        _pending.pop(uid, None)
    while len(_pending) > MAX_PENDING:
        _pending.pop(next(iter(_pending)), None)


def is_roleplay_or_insult(text: str) -> bool:
    """角色扮演 / 侮辱性命名 → 不该写进长期人格。"""
    t = text or ""
    return bool(ROLEPLAY_PATTERN.search(t) or INSULT_PATTERN.search(t))


def looks_like_rename(text: str) -> bool:
    """是否在给她命名/改名。"""
    return bool(RENAME_PATTERN.search(text or ""))


def has_existing_name() -> bool:
    """她是否已有名字（identity 类教训已存在）。

    有名字时再改叫"改名"（要确认）；没有时是"首次命名"（不打扰）。
    """
    from app.models.database import connect

    conn = connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM lessons WHERE kind='identity' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def current_identity_lines(limit: int = 3) -> list[str]:
    """当前的身份类设定（确认文案里要告诉用户"现在是什么"）。"""
    from app.models.database import connect

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT content FROM lessons WHERE kind='identity' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [r["content"] for r in rows]


def check(text: str) -> tuple[str, str]:
    """身份类消息的处理决定。

    返回 (verdict, message)：
    - ("reject", 回复文案)  角色扮演/侮辱，不入库
    - ("confirm", 回复文案) 真的在改名且已有名字，要用户确认
    - ("allow", "")         正常放行（首次命名或非身份消息）
    """
    if not looks_like_rename(text):
        # 非命名语句里的角色扮演（"以后你就是我的猫娘"）也要拦
        if is_roleplay_or_insult(text) and _mentions_self(text):
            return "reject", (
                "这个我就不改了哈，我还是小月。"
                "你要是想调整我的说话方式或习惯，直接说就行。"
            )
        return "allow", ""
    if is_roleplay_or_insult(text):
        return "reject", (
            "这个名字我就不记啦。想换个正经的称呼随时说，"
            "或者你想调我的语气习惯也可以直接讲。"
        )
    if not has_existing_name():
        return "allow", ""  # 首次命名：不打扰
    current = "、".join(current_identity_lines(1)) or "现在的设定"
    return "confirm", (
        f"要改我的名字吗？（现在是：{current}）\n"
        "这条会长期记着，回复「确认」就改，「取消」保持原样。"
    )


def _mentions_self(text: str) -> bool:
    """是否在说"你（她）"——避免把用户吐槽第三方误判成身份污染。"""
    return bool(re.search(r"你|妳|小月", text or ""))


def remember(uid: str, content: str) -> None:
    now = time.time()
    _pending[uid] = {"content": content, "ts": now}
    _prune(now)


def take(uid: str) -> str | None:
    """取出并清除待确认的改名内容（未过期才返回）。"""
    now = time.time()
    _prune(now)
    item = _pending.pop(uid, None)
    if item is None or now - item["ts"] > PENDING_TTL_SECONDS:
        return None
    return item["content"]


def peek(uid: str) -> dict | None:
    _prune(time.time())
    return _pending.get(uid)


def clear(uid: str) -> None:
    _pending.pop(uid, None)


def reset() -> None:
    """测试用。"""
    _pending.clear()
