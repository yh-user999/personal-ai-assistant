"""被动目标追踪：从对话识别意向，不要求用户打命令。

## 动因

`goals` 表长期为 0 条——但不是因为用户没目标，他原话里明明有：
- 「我想在国庆之前减脂减重到62.5KG以下，你帮我规划一下」
- 「我想写小说，我平时想到一些情节内容发给你」

而是因为他**从不打「目标：XXX」这种命令**。同为命令式录入的 `jargon_terms`、
`writing_log` 也全是 0 条，而被动识别的 `concerns` 有 22 条在用——
**被动这条路走得通，命令式走不通**。

## 与 concerns 的区别

- concerns（已有）：你**在意**什么话题 → 影响注入哪些记忆
- goals（这里）：你**想达成**什么 → 需要跟进进展、会完成或放弃

## 噪声控制（最难的部分）

"打算/想/准备"在小说创作语境里满天飞，实测这些都不是目标：
- 「又准备到午休时间了，真快啊」——时间感慨
- 「我是打算原身被打死，原身父亲无力反抗」——在讲剧情设定
- 「外星人…打算研究」——第三人称，说的不是自己

三道闸门：① 必须第一人称且紧跟意向词 ② 排除小说创作语境（含角色名/剧情词）
③ 长度与形态过滤。存为 candidate 而非 active——问过两次没回应就丢弃。
"""
import logging
import re
from datetime import datetime, timedelta, timezone

from app.models.database import connect

logger = logging.getLogger("assistant.intent_goals")

STATUS_CANDIDATE = "candidate"
STATUS_ACTIVE = "active"
STATUS_DONE = "done"
STATUS_DROPPED = "dropped"

SOURCE_PASSIVE = "passive"

# 意向表达：要求第一人称 + 意向词紧邻（"我想学 X" 而不是 "他打算 X"）
INTENT_RE = re.compile(
    # 下限放到 2 字：「我想减脂」「我要健身」都是完整目标，原来卡 4 字全漏了
    r"我(?:想|要|打算|准备|计划|希望)(?!到)(?:要)?\s*([^，,。；;？?！!\n]{2,40})"
    r"|(?:接下来|下一步|之后)我?(?:要|想|准备|打算)\s*([^，,。；;？?！!\n]{2,40})"
)

# 小说创作语境：命中即跳过（用户在讲剧情，不是在定自己的目标）。
# 不能把"小说"本身列进来——「我想写小说」是真目标，实测就被误杀了。
# 判据是**具体的剧情元素**（角色名/情节动作），而非创作这个行为。
NOVEL_CONTEXT_RE = re.compile(
    r"原身|男主|女主|反派|主角|角色设定|人物设定|剧情|情节|"
    r"外星人|修炼体系|命丛|命图|李羽|左志诚|少爷|"
    r"让.{0,6}(?:被打死|死掉|复活|重生)|设定(?:成|为|得)"
)

# 意向内容里的噪声：时间感慨、寒暄、指代不明
NOISE_RE = re.compile(
    r"^(?:到|去|回|睡|吃|走|说|问|看看|试试|再|又)\b"
    r"|午休|下班|上班时间|睡觉|吃饭|休息一下"
    r"|^(?:你|他|她|它|这|那)"
)

# 目标标题最短长度。2 字足够——「减脂」「健身」「写小说」都是完整目标，
# 原来卡 4 字把「我想减脂」这类最典型的表达全漏了。
MIN_TITLE_LEN = 2
# 同一目标的去重阈值（标题前 N 字重合即视为同一个）
DEDUPE_PREFIX = 8
# 追问上限：问过这么多次没回应就丢弃（问两遍就从关心变催促）
MAX_ASK = 2
# 追问间隔：至少隔这么多天才再问
ASK_INTERVAL_DAYS = 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def detect_intents(text: str) -> list[str]:
    """从消息里抽出目标意向。噪声语境返回空。

    语境判断按**抽出的意向内容**而非整条消息——「我想写小说，我平时想到一些
    情节内容发给你」整句含"情节"，但意向本身（"写小说"）是干净的真目标。
    按整句判会误杀（实测就杀掉了）。
    """
    t = (text or "").strip()
    if not t:
        return []
    out: list[str] = []
    for m in INTENT_RE.finditer(t):
        raw = (m.group(1) or m.group(2) or "").strip()
        if not (MIN_TITLE_LEN <= len(raw) <= 40):
            continue
        if NOISE_RE.search(raw) or NOVEL_CONTEXT_RE.search(raw):
            continue
        out.append(raw)
    return out


def _existing_titles(user_id: str) -> list[tuple[int, str, str]]:
    conn = connect()
    try:
        return [
            (r["id"], r["title"], r["status"])
            for r in conn.execute(
                "SELECT id, title, status FROM goals WHERE user_id=? "
                "AND status IN (?, ?)", (user_id, STATUS_CANDIDATE, STATUS_ACTIVE),
            ).fetchall()
        ]
    finally:
        conn.close()


def record_intent(text: str, user_id: str | None = None) -> list[int]:
    """识别并记录候选目标。返回新建的 id 列表（已存在的不重复建）。"""
    from app.core.memory import normalize_user_id

    uid = normalize_user_id(user_id)
    titles = detect_intents(text)
    if not titles:
        return []

    existing = _existing_titles(uid)
    created: list[int] = []
    conn = connect()
    try:
        for title in titles:
            key = title[:DEDUPE_PREFIX]
            if any(key and key in old for _, old, _ in existing):
                continue
            cur = conn.execute(
                "INSERT INTO goals (user_id, title, status, source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (uid, title[:120], STATUS_CANDIDATE, SOURCE_PASSIVE, _now(), _now()),
            )
            created.append(cur.lastrowid)
            existing.append((cur.lastrowid, title, STATUS_CANDIDATE))
        conn.commit()
    finally:
        conn.close()
    if created:
        logger.info("被动识别目标 %d 个: %s", len(created), titles)
    return created


def promote(goal_id: int) -> bool:
    """候选 → 正式（用户回应了追问，说明是真目标）。"""
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE goals SET status=?, updated_at=? WHERE id=? AND status=?",
            (STATUS_ACTIVE, _now(), goal_id, STATUS_CANDIDATE),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def drop(goal_id: int) -> bool:
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE goals SET status=?, updated_at=? WHERE id=?",
            (STATUS_DROPPED, _now(), goal_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def pick_followup(user_id: str | None = None) -> dict | None:
    """挑一个该跟进的候选目标。无合适的返回 None。

    条件：状态 candidate、追问次数未超上限、距上次追问够久。
    追问超上限的自动丢弃——问两遍没回应说明不是真目标（或他不想聊）。
    """
    from app.core.memory import normalize_user_id

    uid = normalize_user_id(user_id)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ASK_INTERVAL_DAYS)).isoformat()
    conn = connect()
    try:
        # 先清理问够了还没回应的
        conn.execute(
            "UPDATE goals SET status=?, updated_at=? "
            "WHERE user_id=? AND status=? AND asked_count >= ?",
            (STATUS_DROPPED, _now(), uid, STATUS_CANDIDATE, MAX_ASK),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, title, asked_count FROM goals WHERE user_id=? AND status=? "
            "AND (last_asked_at IS NULL OR last_asked_at < ?) "
            "ORDER BY created_at ASC LIMIT 1",
            (uid, STATUS_CANDIDATE, cutoff),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def mark_asked(goal_id: int) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE goals SET asked_count = COALESCE(asked_count,0)+1, "
            "last_asked_at=?, updated_at=? WHERE id=?",
            (_now(), _now(), goal_id),
        )
        conn.commit()
    finally:
        conn.close()


def build_injection(user_id: str | None = None) -> str:
    """候选目标追问提示。无可追问的返回空串。

    只提示一个，且明确"问过就别再提"——这类跟进问两遍就变催促。
    """
    item = pick_followup(user_id)
    if not item:
        return ""
    mark_asked(item["id"])
    return (
        f"（用户之前提过想「{item['title']}」，如果当前话题自然接得上，"
        "可以顺口问一句进展怎么样；接不上就别硬提，也别重复问）"
    )


def list_goals(user_id: str | None = None, status: str = "") -> list[dict]:
    from app.core.memory import normalize_user_id

    uid = normalize_user_id(user_id)
    sql = "SELECT * FROM goals WHERE user_id=?"
    args: list = [uid]
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY created_at DESC"
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()
