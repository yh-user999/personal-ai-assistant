"""小说实体索引：专名抽取 + 实体驱动的分层检索。

## 为什么需要这层

实测数据（《寂静杀戮》1936 块）：

| 检索方式 | 命中 | 精度 |
|---|---|---|
| 向量搜「命丛有哪些」 | top3 全是无关 PDF | ≈0（小说排第四，sim 0.023 vs 0.025 无区分力）|
| FTS5 搜类名「命丛」 | 308/1936 | 15.9%，等于没筛 |
| FTS5 搜专名「银河灵潮」 | 1/1936 | ~100% |

两个根因：
1. **枚举式提问与叙事文本语义分布不重叠**——「有哪些」和"当看到那蜷缩起来
   的怪物时"根本不相似，向量检索无能为力。
2. **具体实体有自己的专名，与类名不构成固定组合**。原文里是
   「你的命丛在左眼里，这个命丛，被称之为'夜海'」——专名与类名相隔 8 字；
   也有「命丛夜海」直连、「就剩下夜海这个命丛了」倒序。正则抽不出来
   （实测「XX的命丛」模式的输出全是"而不同/测一测我/单一"这类噪声）。
   而且搜类名会漏：含「夜海」的 69 块里有 26 块不含「命丛」二字。

所以必须先建**专名索引**，把低精度的类名匹配转成高精度的专名匹配。

## 存什么、不存什么

- 存实体表（名字/类型/所属集合/首次位置）——客观索引，不随提问变化，
  问"有哪些"和问"夜海怎么修炼"用同一张表。
- **不存问答结果**——答案缓存会随提问维度爆炸，且不同批次会互相矛盾。
  检索每次重做（纯 SQL 零 LLM，重做不心疼）。
"""
import asyncio
import logging
import re
import sqlite3
from datetime import datetime, timezone

from openai import OpenAIError

from app.models.database import connect

logger = logging.getLogger("assistant.novel_entities")

# ── 实体类型与它们的类名触发词 ──────────────────────────────
ENTITY_KINDS: dict[str, tuple[str, ...]] = {
    "命丛": ("命丛",),
    "命图": ("命图",),
    "功法": ("道术", "功法", "秘籍", "武功", "招式"),
    # 势力的触发词不能用单字「宗」「教」——会命中"宗旨""教训""教会"这类
    # 无关词，实测候选里混进了"孙悟空""恶意""封印"。用双字词组约束。
    "势力": ("门派", "兵团", "宗门", "教派", "帮派", "势力", "组织"),
}

# ── 命名句模式：只有这些块需要交给 LLM 读 ────────────────────
# 中文小说的命名句有稳定特征。用它把 308 块缩到几十块——其余 250 多块
# 是重复提及（"他看着命丛发呆"），读它们不会产出新专名。
# 引号字符类：必须含中文弯引号 ‘’“”（U+2018/2019/201C/201D）。
# 原文写的是「被称之为‘夜海’」，只列 ASCII 的 ' " 会导致命名句全部漏匹配
# ——实测漏掉后命丛候选块从 40+ 掉到 16 块。
_QUOTE = "《》「」『』‘’“”\"'"
_QO = f"[{re.escape(_QUOTE)}]?"

NAMING_PATTERNS = (
    rf"被称之为{_QO}([\u4e00-\u9fff]{{2,8}})",
    rf"称之为{_QO}([\u4e00-\u9fff]{{2,8}})",
    rf"叫做{_QO}([\u4e00-\u9fff]{{2,8}})",
    rf"名为{_QO}([\u4e00-\u9fff]{{2,8}})",
    rf"唤作{_QO}([\u4e00-\u9fff]{{2,8}})",
    rf"[{re.escape(_QUOTE)}]([\u4e00-\u9fff]{{2,8}})[{re.escape(_QUOTE)}]",
)

# 集合性表述：说明存在一个有限集合，用来算"还缺几个"。
# 必须锚定到实体类名——不加约束会把「一大口鲜血」「四大天王」全抽进来
# （实测「一大口鲜血」出现 9 次，比真正的「七大神命丛」还多）。
_GROUP_CLASS = "|".join(w for words in ENTITY_KINDS.values() for w in words)
GROUP_PATTERNS = (
    rf"([一二三四五六七八九十]大[\u4e00-\u9fff]{{0,3}}(?:{_GROUP_CLASS}))",
    rf"([一二三四五六七八九十]种(?:{_GROUP_CLASS}))",
)

# 枚举式提问：这类问法必须走实体检索，向量对它无能为力。
# 「哪些」单独成词即触发——原先写「有哪些」，而用户的真实问法是
# "小说里出现过哪些命丛"（"过哪些"而非"有哪些"），实测漏判。
ENUM_INTENT = re.compile(
    r"哪些|哪几[个种类]|列举|清单|全部|所有的?|都有什么|"
    r"多少[种个位]|几[种个]|汇总|整理一下|盘点|一览|出场"
)

# 定义式提问：「命丛是什么」——走原有向量路径更合适（要的是解释不是清单）
DEFINITION_INTENT = re.compile(r"是什么意思|什么是|是啥|是什么(?![\u4e00-\u9fff])")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 第 0 层：意图与实体识别（零成本）────────────────────────

def detect_enum_intent(query: str) -> bool:
    """是否是枚举式提问。

    保守判定：只认明确的枚举词。「跟我说说命丛」不算——误判会让检索变慢
    且注入变散，而漏判只是退回原有向量路径（现状），代价小得多。
    定义式提问优先排除：「命图是什么意思」要的是解释，不是清单。
    """
    q = query or ""
    if DEFINITION_INTENT.search(q):
        return False
    return bool(ENUM_INTENT.search(q))


def detect_kinds(query: str) -> list[str]:
    """问的是哪几类实体。

    静态四类（命丛/命图/功法/势力）之外，检索自愈登记的动态类名与库内
    已抽取的实体类型同样参与——"炼神有哪些境界"在自动抽取"炼神"类实体后
    能被识别并走实体索引。
    """
    q = query or ""
    kinds = [kind for kind, words in ENTITY_KINDS.items() if any(w in q for w in words)]
    from app.services.knowledge_domain import _dynamic_novel_classes

    for w in _dynamic_novel_classes():
        if w in q and w not in kinds:
            kinds.append(w)
    conn = connect()
    try:
        try:
            db_kinds = [r["kind"] for r in conn.execute(
                "SELECT DISTINCT kind FROM novel_entities"
            ).fetchall()]
        except sqlite3.OperationalError:
            db_kinds = []
    finally:
        conn.close()
    for k in db_kinds:
        if k in q and k not in kinds:
            kinds.append(k)
    return kinds


# ── 实体表 CRUD ───────────────────────────────────────────

def upsert_entity(book: str, name: str, kind: str, *, group_name: str = "",
                  first_chunk: int | None = None, verified: int = 0,
                  note: str = "") -> int:
    """写入实体。已存在则补全缺失字段（不覆盖用户确认过的 note/verified）。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, verified, note, group_name, first_chunk FROM novel_entities "
            "WHERE book=? AND name=? AND kind=?", (book, name, kind)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE novel_entities SET "
                "group_name = CASE WHEN ?!='' THEN ? ELSE group_name END, "
                "first_chunk = COALESCE(first_chunk, ?), "
                # verified/note 只升不降：用户确认过的不被后续自动抽取覆盖
                "verified = MAX(verified, ?), "
                "note = CASE WHEN ?!='' THEN ? ELSE note END "
                "WHERE id=?",
                (group_name, group_name, first_chunk, verified, note, note, row["id"]),
            )
            conn.commit()
            return row["id"]
        cur = conn.execute(
            "INSERT INTO novel_entities (book, name, kind, group_name, first_chunk, "
            "verified, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (book, name, kind, group_name, first_chunk, verified, note, _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_entities(book: str = "", kind: str = "",
                  verified_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM novel_entities WHERE 1=1"
    args: list = []
    if book:
        sql += " AND book=?"
        args.append(book)
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    if verified_only:
        sql += " AND verified=1"
    sql += " ORDER BY kind, first_chunk"
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def search_entities(
    query: str,
    entity_kind: str | None = None,
    book: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """按名称/类别/所属书检索实体，供外部只读入口使用。"""
    term = (query or "").strip()
    if not term:
        raise ValueError("实体查询不能为空")
    limit = max(1, min(int(limit), 100))
    where = ["(name LIKE ? OR note LIKE ? OR group_name LIKE ? OR kind LIKE ? OR book LIKE ?)"]
    like = f"%{term}%"
    args: list[object] = [like, like, like, like, like]
    if entity_kind:
        where.append("kind=?")
        args.append(entity_kind.strip())
    if book:
        where.append("book=?")
        args.append(book.strip())
    args.append(limit)
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, name, kind, book, group_name, first_chunk, verified, note, created_at "
            f"FROM novel_entities WHERE {' AND '.join(where)} "
            "ORDER BY verified DESC, kind, first_chunk, id LIMIT ?",
            args,
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def delete_entity(entity_id: int) -> bool:
    conn = connect()
    try:
        cur = conn.execute("DELETE FROM novel_entities WHERE id=?", (entity_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def verify_entity(book: str, name: str, kind: str, note: str = "") -> bool:
    """用户确认某个实体（可附修订说明，修订优先于原文）。"""
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE novel_entities SET verified=1, "
            "note = CASE WHEN ?!='' THEN ? ELSE note END "
            "WHERE book=? AND name=? AND kind=?",
            (note, note, book, name, kind),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── 阶段一：命名句定位（纯规则，把 308 块缩到几十块）──────────

# 类名与专名的最大间隔：原文「这个命丛，被称之为'夜海'」相隔 8 字，
# 放宽到 40 字覆盖「你的命丛在左眼里，这个命丛，被称之为X」这类插入语
KIND_NAME_WINDOW = 40


def find_naming_blocks(book: str, kind: str) -> list[dict]:
    """含命名句的块（候选集）。返回 [{chunk_index, content, hints}]。

    hints 是正则预抽的候选名，仅供 LLM 参考——**不直接入库**，
    因为实测正则噪声极高（「XX的命丛」抽出"而不同/测一测我/单一"）。
    """
    class_words = ENTITY_KINDS.get(kind, (kind,))
    like_clause = " OR ".join("content LIKE ?" for _ in class_words)
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT chunk_index, content FROM knowledge_chunks "
            f"WHERE doc_name=? AND ({like_clause}) ORDER BY chunk_index",
            (book, *[f"%{w}%" for w in class_words]),
        ).fetchall()
    finally:
        conn.close()

    out = []
    for r in rows:
        text = r["content"]
        hints = _naming_hints(text, class_words)
        if hints:
            out.append({
                "chunk_index": r["chunk_index"],
                "content": text,
                "hints": sorted(hints),
            })
    return out


def _naming_hints(text: str, class_words: tuple[str, ...]) -> set[str]:
    """块内的候选专名：命名句模式命中、且离类名足够近。

    距离约束是关键——书里到处有「被称之为南圣门」这类与命丛无关的命名句，
    不加约束会把全书专名都抽进来。
    """
    hits: set[str] = set()
    kind_positions = [
        m.start() for w in class_words for m in re.finditer(re.escape(w), text)
    ]
    if not kind_positions:
        return hits
    for pat in NAMING_PATTERNS:
        for m in re.finditer(pat, text):
            name = (m.group(1) or "").strip()
            if not _plausible_name(name):
                continue
            if any(abs(m.start() - kp) <= KIND_NAME_WINDOW for kp in kind_positions):
                hits.add(name)
    return hits


# 明显不是专名的词：常见动词/量词/连接词开头，或整体是常用语
_NAME_STOPWORDS = frozenset({
    "什么", "这个", "那个", "一个", "两个", "自己", "他们", "我们", "你们",
    "而已", "之一", "的话", "的人", "的事", "方法", "能力", "力量", "时候",
    "东西", "地方", "样子", "意思", "问题", "情况", "已经", "或者", "不过",
    "但是", "因为", "所以", "如果", "虽然", "然后", "接着", "于是",
})


def _plausible_name(name: str) -> bool:
    """形态过滤：长度合理、非停用词、不含数字标点。"""
    if not (2 <= len(name) <= 8):
        return False
    if name in _NAME_STOPWORDS:
        return False
    if any(name.startswith(w) for w in ("的", "了", "是", "有", "在", "和")):
        return False
    return bool(re.fullmatch(r"[\u4e00-\u9fff]+", name))


# ── 枚举句直抽（补命名句模式的漏）─────────────────────────────
# 命名句模式只覆盖「被称之为X」这类逐个介绍的写法，漏掉了作者一次列举
# 多个的句子：「这四种命图分别是鬼眼黄泉天，清净焰光城，夜亡君主以及
# 白帝极光剑」——实测这一句里的 4 个命图只抽到 2 个。
ENUM_SENTENCE = re.compile(
    r"(?:分别是|分成|分为|包括|有|是)([^。！？\n]{4,120})"
)
# 列举分隔符（中文顿号/逗号/以及/和/还有）
LIST_SPLIT = re.compile(r"[，,、]|以及|还有|和(?![\u4e00-\u9fff]{0,2}的)")


def find_enumerated_names(book: str, kind: str) -> dict[str, int]:
    """从枚举句里直抽专名：{name: chunk_index}。

    只信"类名 + 枚举句"同时出现的句子，且逐项做形态过滤——
    这层是补漏，不是主力，宁可少抽也不能把叙事句里的词当专名。
    """
    class_words = ENTITY_KINDS.get(kind, (kind,))
    like_clause = " OR ".join("content LIKE ?" for _ in class_words)
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT chunk_index, content FROM knowledge_chunks "
            f"WHERE doc_name=? AND ({like_clause}) ORDER BY chunk_index",
            (book, *[f"%{w}%" for w in class_words]),
        ).fetchall()
    finally:
        conn.close()

    found: dict[str, int] = {}
    for r in rows:
        for sent in _split_sentences(r["content"]):
            if not any(w in sent for w in class_words):
                continue
            for m in ENUM_SENTENCE.finditer(sent):
                items = [s.strip() for s in LIST_SPLIT.split(m.group(1))]
                # 枚举至少 2 项才算列举（单项容易是叙事句误命中）
                valid = [s for s in items if _plausible_name(s)]
                if len(valid) >= 2:
                    for name in valid:
                        found.setdefault(name, r["chunk_index"])
    return found


def find_group_mentions(book: str) -> dict[str, int]:
    """集合性表述及出现次数：{「七大神命丛」: 3}。

    用来算缺口——原文说"七大神命丛"而实体表只有 5 个，就该告诉用户还缺 2 个。
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT content FROM knowledge_chunks WHERE doc_name=?", (book,)
        ).fetchall()
    finally:
        conn.close()
    counts: dict[str, int] = {}
    for r in rows:
        for pat in GROUP_PATTERNS:
            for m in re.finditer(pat, r["content"]):
                key = m.group(1)
                counts[key] = counts.get(key, 0) + 1
    return counts


GROUP_SIZE_CN = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def parse_group_size(group_name: str) -> int | None:
    """「七大神命丛」→ 7；解析不出返回 None。"""
    m = re.search(r"([一二三四五六七八九十])大", group_name or "")
    if m:
        return GROUP_SIZE_CN.get(m.group(1))
    m = re.search(r"([一二三四五六七八九十])种", group_name or "")
    if m:
        return GROUP_SIZE_CN.get(m.group(1))
    return None


# ── 阶段一（续）：LLM 抽专名 ───────────────────────────────

# 抽取并发度：太高会被 API 限速，太低跑不完（58 块串行超过 15 分钟）
EXTRACT_CONCURRENCY = 6

EXTRACT_PROMPT = """从下面的小说片段里找出所有「{kind}」的**专有名称**。

只输出 JSON：{{"names": ["名称1", "名称2"]}}

判断标准：
- 专名指某个具体的{kind}的名字，例如原文「这个命丛，被称之为'夜海'」里的"夜海"
- 类名本身（"{kind}"二字）不算专名，不要输出
- 人名、地名、门派名、功法名不是{kind}的名字，不要输出
- 只输出**原文明确指为{kind}**的名称；拿不准就不要输出（宁缺勿滥）
- 名称要完整且干净：输出"鬼眼黄泉天"而不是"鬼眼黄泉天的命图"

正则预抽的候选（仅供参考，含大量噪声，需你判断真假）：
{hints}

小说片段：
{text}

只输出 JSON，不要 markdown 代码块。"""


async def extract_entities(book: str, kind: str, *, dry_run: bool = False,
                           max_blocks: int = 40) -> dict:
    """从命名句候选块里抽专名。

    dry_run=True 只返回抽取结果不入库——先给用户过一遍再决定（LLM 会误抽，
    比如把人名当命丛名）。确认后调 confirm_extracted 入库。
    """
    from app.core import llm

    blocks = find_naming_blocks(book, kind)[:max_blocks]
    # 补上枚举句所在的块：命名句模式只覆盖"逐个介绍"的写法，漏掉作者一次
    # 列举多个的句子（「这四种命图分别是鬼眼黄泉天，清净焰光城，夜亡君主
    # 以及白帝极光剑」——实测这句里 4 个命图只抽到 2 个）。
    # 枚举句直抽本身噪声极高（命丛 488 个候选多是句子片段），所以只用它
    # **定位块**并作为 hints 交给 LLM 判真假，不直接入库。
    enum_hits = find_enumerated_names(book, kind)
    enum_chunks: dict[int, set[str]] = {}
    for name, cidx in enum_hits.items():
        enum_chunks.setdefault(cidx, set()).add(name)
    known = {b["chunk_index"] for b in blocks}
    for cidx, names in sorted(enum_chunks.items()):
        if cidx in known:
            # 已在候选里：把枚举候选并入该块的 hints
            for b in blocks:
                if b["chunk_index"] == cidx:
                    b["hints"] = sorted(set(b["hints"]) | names)
                    break
        elif len(blocks) < max_blocks:
            content = _fetch_chunk(book, cidx)
            if content:
                blocks.append({"chunk_index": cidx, "content": content,
                               "hints": sorted(names)})
    blocks.sort(key=lambda b: b["chunk_index"])

    if not blocks:
        return {"kind": kind, "names": [], "blocks": 0, "reason": "无命名句候选块"}

    # 并发抽取：逐块串行时 58 块 × 数秒/块 会跑十几分钟（实测超 15 分钟未完）。
    # 限流是必须的——一次性 gather 60 个请求会被 API 端限速或直接拒绝。
    sem = asyncio.Semaphore(EXTRACT_CONCURRENCY)

    async def _one(b: dict) -> tuple[int, list[str]]:
        prompt = EXTRACT_PROMPT.format(
            kind=kind,
            hints="、".join(b["hints"]) or "（无）",
            text=b["content"][:1800],
        )
        async with sem:
            try:
                result = await llm.chat_json(
                    "你是小说设定提取助手，只输出 JSON。", prompt
                )
            except OpenAIError as e:
                logger.warning("实体抽取失败 chunk#%s: %s", b["chunk_index"], e)
                return b["chunk_index"], []
        return b["chunk_index"], [str(n).strip() for n in (result.get("names") or [])]

    results = await asyncio.gather(*(_one(b) for b in blocks))

    found: dict[str, int] = {}  # name → first_chunk（按块序保序，取最早出现）
    for cidx, names in sorted(results, key=lambda kv: kv[0]):
        for name in names:
            if _plausible_name(name) and name not in ENTITY_KINDS.get(kind, ()):
                found.setdefault(name, cidx)

    # 集合归属：原文说「七大神命丛」就把该类实体挂上去，用来算缺口
    groups = find_group_mentions(book)
    group_name = ""
    for g in groups:
        if kind in g:
            group_name = g
            break

    payload = {
        "book": book,
        "kind": kind,
        "blocks": len(blocks),
        "group_name": group_name,
        "group_size": parse_group_size(group_name),
        "names": [{"name": n, "first_chunk": c} for n, c in sorted(found.items(),
                                                                  key=lambda kv: kv[1])],
    }
    if not dry_run:
        confirm_extracted(payload)
    return payload


def confirm_extracted(payload: dict, verified: int = 0) -> int:
    """把抽取结果写入实体表。返回写入条数。"""
    book, kind = payload["book"], payload["kind"]
    group_name = payload.get("group_name", "")
    n = 0
    for item in payload.get("names", []):
        upsert_entity(
            book, item["name"], kind,
            group_name=group_name,
            first_chunk=item.get("first_chunk"),
            verified=verified,
        )
        n += 1
    logger.info("实体入库 %s/%s：%d 条", book, kind, n)
    return n


def resolve_cross_kind_duplicates(book: str) -> list[dict]:
    """同名实体被判进多个类型时，归属证据更强的那一类。

    实测「夜海」被同时抽成命丛和命图——它是命丛，只因常与命图一起讨论
    （"夜海作为神命丛，其所能选择的命图并不多"）而被误判。
    判据用共现频次：正文里「命丛夜海」「夜海…命丛」的共现次数远高于命图。
    verified=1 的记录不动（用户确认过的权威）。
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT name, COUNT(*) AS c FROM novel_entities WHERE book=? "
            "GROUP BY name HAVING c > 1", (book,)
        ).fetchall()
        dupes = [r["name"] for r in rows]
    finally:
        conn.close()

    removed = []
    for name in dupes:
        cands = [e for e in list_entities(book=book) if e["name"] == name]
        if any(e["verified"] for e in cands):
            continue  # 用户已确认，不自动处理
        # 枚举句归属优先于共现频次：共现在这里会误判——「鬼眼黄泉天」是命图，
        # 但它需要 81 个命丛，正文里与"命丛"共现 85 次、与"命图"仅 31 次。
        # 而 #385「这四种命图分别是鬼眼黄泉天，清净焰光城…」是直接证据。
        enum_kinds = [e["kind"] for e in cands
                      if name in find_enumerated_names(book, e["kind"])]
        if len(enum_kinds) == 1:
            best = enum_kinds[0]
            scores = {"枚举句直接归属": best}
        else:
            scores = {e["kind"]: _cooccurrence_score(book, name, e["kind"])
                      for e in cands}
            best = max(scores, key=lambda k: scores[k])
        for e in cands:
            if e["kind"] != best:
                delete_entity(e["id"])
                removed.append({"name": name, "dropped_kind": e["kind"],
                                "kept_kind": best, "scores": scores})
    if removed:
        logger.info("跨类去重 %d 条: %s", len(removed),
                    [(r["name"], r["dropped_kind"]) for r in removed])
    return removed


# 专名与类名的共现窗口（判断"夜海"更像命丛还是命图）
COOC_WINDOW = 24


def _cooccurrence_score(book: str, name: str, kind: str) -> int:
    """专名与该类类名在 COOC_WINDOW 字内共现的次数。"""
    class_words = ENTITY_KINDS.get(kind, (kind,))
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT content FROM knowledge_chunks WHERE doc_name=? AND content LIKE ?",
            (book, f"%{name}%"),
        ).fetchall()
    finally:
        conn.close()
    score = 0
    for r in rows:
        text = r["content"]
        for m in re.finditer(re.escape(name), text):
            lo = max(0, m.start() - COOC_WINDOW)
            hi = min(len(text), m.end() + COOC_WINDOW)
            window = text[lo:hi]
            score += sum(window.count(w) for w in class_words)
    return score


# ── 第 2 层：块内定位裁剪 ──────────────────────────────────
# 治的是"注入 5638 字符却全是冥王蛇登场时马匹嘶鸣的描写"——
# 命中一块 1500 字后只取含专名的句子，不要整块。

SENTENCE_SPLIT = re.compile(r"[。！？\n]+")
CONTEXT_SENTENCES = 1     # 专名所在句的前后各取几句
MAX_SNIPPET_CHARS = 220   # 单个片段上限

# 枚举性动词：与专名共现时说明这句在"定义/列举"，信息密度远高于叙事句
# （「道术修炼一共分为炼命丛、修天宫…」优于「他看着命丛发呆」）
INFORMATIVE_WORDS = (
    "分为", "分成", "共有", "包括", "要求", "称之为", "叫做", "名为",
    "之一", "一共", "总共", "需要", "作用", "能力", "效果", "特性",
)


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in SENTENCE_SPLIT.split(text or "") if s.strip()]


def extract_snippet(content: str, name: str) -> tuple[str, float]:
    """块内裁剪：取含专名的句子 ±CONTEXT_SENTENCES 句。返回 (片段, 分数)。"""
    sentences = _split_sentences(content)
    idxs = [i for i, s in enumerate(sentences) if name in s]
    if not idxs:
        return "", 0.0
    lo = max(0, idxs[0] - CONTEXT_SENTENCES)
    hi = min(len(sentences), idxs[-1] + CONTEXT_SENTENCES + 1)
    window = sentences[lo:hi]
    snippet = "。".join(window)
    if len(snippet) > MAX_SNIPPET_CHARS:
        # 超长时只保留命中句本身及其后一句
        core = sentences[idxs[0]: min(len(sentences), idxs[0] + 2)]
        snippet = "。".join(core)[:MAX_SNIPPET_CHARS]

    score = float(len(idxs))  # 专名出现次数
    score += sum(2.0 for w in INFORMATIVE_WORDS if w in snippet)  # 定义性表述加权
    return snippet, score


# ── 第 1/3/4 层：实体驱动检索 → 聚合去重 → 预算裁剪 ───────────

MAX_SNIPPETS_PER_ENTITY = 2   # 每个专名最多留几个片段
TOTAL_BUDGET_CHARS = 2600     # 注入总预算


def _fetch_chunk(book: str, chunk_index: int) -> str:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT content FROM knowledge_chunks WHERE doc_name=? AND chunk_index=?",
            (book, chunk_index),
        ).fetchone()
    finally:
        conn.close()
    return row["content"] if row else ""


def _fetch_blocks_by_name(book: str, name: str, limit: int = 12) -> list[dict]:
    """按专名精确检索。精度对比：搜类名「命丛」命中 308/1936（15.9%），
    搜专名「银河灵潮」命中 1 块（~100%）。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT chunk_index, content FROM knowledge_chunks "
            "WHERE doc_name=? AND content LIKE ? ORDER BY chunk_index LIMIT ?",
            (book, f"%{name}%", limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _dedupe(snippets: list[dict]) -> list[dict]:
    """同一事实在书里反复出现——相似片段只留分数最高的。

    判据是前 30 字重合（小说里同一段设定常被逐字重复引用）。
    """
    seen: dict[str, dict] = {}
    for s in sorted(snippets, key=lambda x: -x["score"]):
        key = re.sub(r"\s", "", s["text"])[:30]
        if key not in seen:
            seen[key] = s
    return sorted(seen.values(), key=lambda x: -x["score"])


def build_entity_context(query: str, book: str = "") -> str:
    """枚举式提问的实体检索注入。非枚举意图返回空串（走原有向量路径）。

    输出含**完整度报告**——让她能诚实说"确认 N 个、原文提到该有 M 个"，
    而不是笼统地"没有可靠依据不敢硬凑"。
    """
    if not detect_enum_intent(query):
        return ""
    kinds = detect_kinds(query)
    if not kinds:
        return ""

    books = [book] if book else _books_with_entities()
    sections: list[str] = []
    for bk in books:
        for kind in kinds:
            section = _build_kind_section(bk, kind, query)
            if section:
                sections.append(section)
    return "\n\n".join(sections)


def _books_with_entities() -> list[str]:
    conn = connect()
    try:
        return [r["book"] for r in conn.execute(
            "SELECT DISTINCT book FROM novel_entities"
        ).fetchall()]
    finally:
        conn.close()


def _build_kind_section(book: str, kind: str, query: str) -> str:
    entities = list_entities(book=book, kind=kind)
    if not entities:
        return ""

    lines: list[str] = []
    used = 0
    detailed = 0
    for ent in entities:
        name = ent["name"]
        # 用户修订优先于原文（verified 的 note 是权威版本）
        if ent.get("note"):
            lines.append(f"{name}（你修订过）：{ent['note']}")
            detailed += 1
            used += len(ent["note"])
            continue
        snippets = []
        for blk in _fetch_blocks_by_name(book, name):
            text, score = extract_snippet(blk["content"], name)
            if text:
                snippets.append({"text": text, "score": score,
                                 "chunk": blk["chunk_index"]})
        snippets = _dedupe(snippets)[:MAX_SNIPPETS_PER_ENTITY]
        if not snippets:
            lines.append(f"{name}（实体表有名字，但正文未找到描述）")
            continue
        detailed += 1
        for s in snippets:
            if used + len(s["text"]) > TOTAL_BUDGET_CHARS:
                break
            lines.append(f"{name}（#{s['chunk']}）：{s['text']}")
            used += len(s["text"])

    if not lines:
        return ""

    # 完整度报告：缺口可见才谈得上诚实
    group_name = next((e["group_name"] for e in entities if e.get("group_name")), "")
    expected = parse_group_size(group_name) if group_name else None
    report = f"实体表收录 {len(entities)} 个，其中 {detailed} 个找到正文描述"
    if expected:
        report += f"；原文提到「{group_name}」应有 {expected} 个"
        if len(entities) < expected:
            report += f"，仍缺 {expected - len(entities)} 个未在正文中命名"
    # 注入里不用 "- " 开头：LLM 会照抄注入的格式输出。实测注入里有 24 行
    # "- "，她的回复就满是 Markdown 列表和 **加粗**——而 QQ 不渲染 Markdown，
    # 用户看到的是一堆字面的星号和减号。**注入格式在示范她该怎么写**，
    # 这比 prompt 里的禁令更有说服力。
    return (
        f"【{book} · {kind}清单（实体索引检索，可直接作为回答依据）】\n"
        f"（{report}。回答时如实说明缺口，不要凑数。"
        f"转述时用自然语句，不要用 Markdown 列表或加粗——用户端不渲染）\n"
        + "\n".join(lines)
    )
