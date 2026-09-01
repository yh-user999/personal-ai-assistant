"""知识库主动利用：你灌了 3520 块但很少被问到。

## 动因

知识库有两本小说、教程、项目文档，但检索是**被动的**——只在明确提问时才用。
用户聊到某个话题时，库里其实有相关资料，他自己不知道可以问。

## 设计原则：提示可用，不替他决定

这个功能最大的风险是变成打扰（"你还没问我这个呢"）。所以：
- 只在**当前话题与库里资料有实质交集**时提示
- 一次只提一个方向，且明确"接不上就别硬提"
- 已经在回答该话题时不提示（检索已经在用了，再提是废话）
- 同一资料短期内不重复提示

零 LLM：FTS5 命中数 + 话题词交集。
"""
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from app.models.database import connect

logger = logging.getLogger("assistant.khint")

# 提示冷却：同一文档多久内不重复提（避免变成推销）
HINT_COOLDOWN_SECONDS = 6 * 3600
# 全局冷却：任何提示之间至少隔多久（一天最多几次，别话痨）
GLOBAL_COOLDOWN_SECONDS = 2 * 3600
# 命中门槛：库里至少这么多块相关才值得提。
# 3 块太低——泛泛的日常词也能凑到（实测「今天天气不错」误触发）。
# 提到 30：真正成体系的资料（小说角色/专有概念）轻松过线，闲聊词过不了。
MIN_RELATED_CHUNKS = 30
# 单个话题词至少要在库里命中这么多块，才算"有实质交集"的词
MIN_TERM_CHUNKS = 8
# 话题词最短长度（避免"的""了"这类）
MIN_TERM_LEN = 2

# 内存态冷却记录：{doc_name: 上次提示时间}。重启清零可接受——
# 这是"别烦人"的软约束，不是必须持久化的业务数据。
_last_hinted: dict[str, float] = {}
_last_any_hint: float = 0.0

# 已经在直接提问的信号：这时检索已经在用库了，提示是废话
ASKING_RE = re.compile(
    r"有哪些|是什么|怎么|为什么|哪个|查一下|搜一下|知识库|资料|说说|讲讲|介绍"
)

# 从消息里抽话题词用的停用词
_STOP = frozenset({
    "我们", "你们", "他们", "自己", "现在", "今天", "昨天", "明天", "这个",
    "那个", "什么", "怎么", "为什么", "可以", "应该", "已经", "还是", "但是",
    "然后", "所以", "因为", "如果", "感觉", "觉得", "知道", "看看", "有点",
})


def _terms(text: str) -> list[str]:
    """抽出可用于检索的实词。

    **不做任意切分**：中文没有分词器，按固定字数切会产出「左志诚这段不」
    这类搜不到东西的片段（实测切出来的 4 个词全部 0 命中），或者反过来
    乱撞命中（「反代」的碎片匹配到了小说）。
    正确做法是只用**已知的实体名**——实体表已有 160 个专名，它们天然是
    "库里成体系的话题词"，命中即有实质交集。
    """
    t = text or ""
    out: list[str] = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9_+-]{2,}", t)]
    known = _known_names()
    out.extend(n for n in known if len(n) >= MIN_TERM_LEN and n in t)
    return out[:8]


def _known_names() -> set[str]:
    """已知专名：实体表 + 设定卡触发词 + 人物别名。

    实体表只抽了命丛/命图/功法/势力，**没有人物**——「左志诚」这类最常聊的
    名字不在里面（实测就漏了）。人物名在 novel_facts 的 keywords 与
    NOVEL_ALIASES 里，必须一起纳入。
    """
    from app.core.knowledge import NOVEL_ALIASES

    names: set[str] = set()
    conn = connect()
    try:
        names.update(r["name"] for r in conn.execute(
            "SELECT DISTINCT name FROM novel_entities"
        ).fetchall())
        for r in conn.execute("SELECT keywords FROM novel_facts").fetchall():
            names.update(
                k.strip() for k in (r["keywords"] or "").replace("，", ",").split(",")
                if len(k.strip()) >= MIN_TERM_LEN
            )
    finally:
        conn.close()
    for alias, alts in NOVEL_ALIASES.items():
        names.add(alias)
        names.update(alts)
    return {n for n in names if n}


def _related_docs(terms: list[str]) -> list[tuple[str, int]]:
    """{文档: 命中块数}，按命中数降序。只看 novel/manual 域——
    项目文档他自己写的，不需要提示；简历也不需要。"""
    if not terms:
        return []
    conn = connect()
    try:
        counts: dict[str, int] = {}
        for term in terms:
            rows = conn.execute(
                "SELECT doc_name, COUNT(*) AS n FROM knowledge_chunks "
                "WHERE domain IN ('novel','manual') AND content LIKE ? "
                "GROUP BY doc_name", (f"%{term}%",),
            ).fetchall()
            for r in rows:
                # 单词命中太少的不计入：说明它不是这本书的实质话题词
                if r["n"] < MIN_TERM_CHUNKS:
                    continue
                counts[r["doc_name"]] = counts.get(r["doc_name"], 0) + r["n"]
    finally:
        conn.close()
    return sorted(
        [(d, n) for d, n in counts.items() if n >= MIN_RELATED_CHUNKS],
        key=lambda kv: -kv[1],
    )


def build_hint(message: str, *, now: float | None = None) -> str:
    """当前话题的可用资料提示。不合适时返回空串。"""
    global _last_any_hint

    text = message or ""
    if not text.strip() or ASKING_RE.search(text):
        return ""  # 已经在问了，检索会处理，提示是废话
    now = now if now is not None else time.time()
    if now - _last_any_hint < GLOBAL_COOLDOWN_SECONDS:
        return ""

    docs = _related_docs(_terms(text))
    for doc, n in docs:
        if now - _last_hinted.get(doc, 0.0) < HINT_COOLDOWN_SECONDS:
            continue
        _last_hinted[doc] = now
        _last_any_hint = now
        return (
            f"（知识库里《{doc}》有 {n} 处与当前话题相关的内容，他还没问。"
            "如果聊到能自然接上的地方，可以提一句可以帮他查；接不上就别提，"
            "更不要罗列资料内容）"
        )
    return ""


def reset() -> None:
    """测试用：清空冷却状态。"""
    global _last_any_hint
    _last_hinted.clear()
    _last_any_hint = 0.0
