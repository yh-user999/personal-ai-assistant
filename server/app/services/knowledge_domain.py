"""知识库分域：先严格限定域，无结果再全域兜底。

## 为什么需要

实测（3602 块，19 个文档混在一张表里）：

| 提问 | 命中的无关文档 |
|---|---|
| 「李羽的能力是什么」（《寂静杀戮》角色）| **6/6 全错**——4 块来自另一本小说、2 块来自 LESSONS.md |
| 「命丛有哪些」| 3/6——反代教程 PDF、AI 优化模板、名词焦虑 PDF |

根因不是检索算法写错了，是 **embedding 各向异性**：实测所有块的相似度都塌在
0.023~0.025 这个 0.002 宽的区间里（正常应有明显梯度：相关 0.7+、无关 0.3-）。
向量对"相关/无关"没有区分力，`min_similarity=0.35` 这个配置形同虚设——
没有任何块能达到，靠阈值过滤会一条都返回不了。

既然向量分不出来，就用**元数据过滤**兜住：问小说时只搜小说，问项目时只搜文档。

## LESSONS.md 是特殊污染源

它反复出现在剧情问题的命中里，因为里面写满了「左志诚被谁挖走了命丛」这类
**用来举例的剧情文字**——我们写的踩坑文档变成了检索噪声（自我污染）。
归到 project_doc 域后被自动隔离。
"""
import logging
import re

from app.models.database import connect

logger = logging.getLogger("assistant.kdomain")

# ── 域定义 ────────────────────────────────────────────────
DOMAIN_NOVEL = "novel"
DOMAIN_PROJECT = "project_doc"
DOMAIN_MANUAL = "manual"
DOMAIN_RESUME = "resume"
DOMAIN_OTHER = "other"

ALL_DOMAINS = (DOMAIN_NOVEL, DOMAIN_PROJECT, DOMAIN_MANUAL, DOMAIN_RESUME, DOMAIN_OTHER)

# 哨兵：明确"不该检索知识库"，区别于空列表（= 判不出域，应走全域）。
# 少了这个区分，facts 已覆盖的主体会退回全域检索并捞回一堆噪声。
SKIP_SEARCH = "__skip__"

# 文档名 → 域的判定规则（按顺序匹配，首个命中即用）
_DOC_RULES: tuple[tuple[str, str], ...] = (
    (r"^小说[-－]", DOMAIN_NOVEL),
    (r"简历", DOMAIN_RESUME),
    (r"教程|必看|指南|手册|入门", DOMAIN_MANUAL),
    # 项目文档：全大写英文名（LESSONS/OPS/REFERENCES…）或含项目术语的中文名
    (r"^[A-Z][A-Z_]+$", DOMAIN_PROJECT),
    (r"实施方案|设计|评审|进度|部署|运维|测试", DOMAIN_PROJECT),
)


def classify_doc(doc_name: str) -> str:
    """文档名 → 域。"""
    name = doc_name or ""
    for pattern, domain in _DOC_RULES:
        if re.search(pattern, name):
            return domain
    return DOMAIN_OTHER


# ── 查询意图 → 目标域 ─────────────────────────────────────

# 项目术语：命中即判为问项目（这些词不会出现在小说里）
_PROJECT_TERMS = re.compile(
    r"RAG|rag|向量|embedding|检索|注入|prompt|知识库|执行器|采集器|"
    r"定时任务|备份|脱敏|白名单|周报|画像|测试|部署|服务器|接口|"
    r"数据库|SQLite|sqlite|token|LLM|llm|命令族|回归集"
)
_RESUME_TERMS = re.compile(r"简历|求职|岗位|面试|工作经历")
_MANUAL_TERMS = re.compile(r"教程|反代|新手|怎么装|如何配置")


def _novel_names() -> dict[str, set[str]]:
    """{书名: 该书的专名集合}。

    书名来自 knowledge_chunks（凡是 novel 域的文档都算），不能只从
    novel_entities 读——**没抽过实体的书会完全无法按书名定位**。
    专名来自实体表（可能为空集）。
    """
    conn = connect()
    try:
        books = [r["doc_name"] for r in conn.execute(
            "SELECT DISTINCT doc_name FROM knowledge_chunks WHERE domain=?",
            (DOMAIN_NOVEL,),
        ).fetchall()]
        out: dict[str, set[str]] = {b: set() for b in books}
        for r in conn.execute("SELECT book, name FROM novel_entities").fetchall():
            out.setdefault(r["book"], set()).add(r["name"])
    finally:
        conn.close()
    return out


def _novel_class_words() -> set[str]:
    """小说体系的类名（命丛/命图/道术…）——它们不是专名但同样能定位到小说域。

    「命丛有哪些」这种问法里没有任何专名，靠专名匹配判不出域，而这恰恰是
    污染最严重的问法（实测命中反代教程 PDF、AI 模板、名词焦虑 PDF）。
    """
    from app.services.novel_entities import ENTITY_KINDS

    words = set(ENTITY_KINDS.keys())
    for group in ENTITY_KINDS.values():
        words.update(w for w in group if len(w) >= 2)
    return words


def _novel_person_names() -> dict[str, set[str]]:
    """{书名: 人物名集合}。人物名来自小说设定卡与别名表——实体表只抽了
    命丛/命图/功法/势力，没有人物，而「李羽的能力是什么」全靠人物名定位。"""
    from app.core.knowledge import NOVEL_ALIASES

    conn = connect()
    try:
        rows = conn.execute("SELECT book, keywords FROM novel_facts").fetchall()
    finally:
        conn.close()
    out: dict[str, set[str]] = {}
    for r in rows:
        names = {k.strip() for k in (r["keywords"] or "").replace("，", ",").split(",")
                 if len(k.strip()) >= 2}
        out.setdefault(r["book"], set()).update(names)
    # 别名表里的人物名（左志诚=左擎苍）——归到所有小说域（无书归属信息）
    alias_names = set()
    for k, alts in NOVEL_ALIASES.items():
        alias_names.add(k)
        alias_names.update(alts)
    if alias_names:
        out.setdefault("", set()).update(n for n in alias_names if len(n) >= 2)
    return out


# 体系词归属某本书的判据：该书的出现次数占全部的比例下限。
# 「命丛」在《寂静杀戮》出现 308 块、另一本 0 块 → 独占，只搜前者。
# 若两本书都大量出现（如"道术"），则不收窄，保留两本。
CLASS_WORD_DOMINANCE = 0.9


# 体系词→书籍归属的缓存。这份映射只在灌入新书时才变，而 detect_domains
# 每轮聊天都调用——实测未缓存时稳态 48ms/轮（首次 828ms，SQLite 页缓存冷），
# 因为要对 3520 块做全表 LIKE 扫描。按知识库块数做失效判据：块数变了说明
# 灌过新内容，重新计算；否则直接复用。
_class_book_cache: dict[str, list[str]] = {}
_class_book_cache_key: int | None = None


def _chunk_count() -> int:
    conn = connect()
    try:
        return conn.execute("SELECT COUNT(*) AS c FROM knowledge_chunks").fetchone()["c"]
    finally:
        conn.close()


def invalidate_cache() -> None:
    """灌库后调用（ingest_document 会触发）。"""
    global _class_book_cache_key
    _class_book_cache.clear()
    _class_book_cache_key = None


def _books_for_class_words(words: list[str]) -> list[str]:
    """体系词（命丛/命图）→ 独占它的书。无明显归属时返回空（不收窄）。"""
    global _class_book_cache_key

    count = _chunk_count()
    if _class_book_cache_key != count:
        _class_book_cache.clear()
        _class_book_cache_key = count
    key = "|".join(sorted(words))
    if key in _class_book_cache:
        return _class_book_cache[key]

    result = _compute_books_for_class_words(words)
    _class_book_cache[key] = result
    return result


def _compute_books_for_class_words(words: list[str]) -> list[str]:
    conn = connect()
    try:
        books = [r["doc_name"] for r in conn.execute(
            "SELECT DISTINCT doc_name FROM knowledge_chunks WHERE domain=?",
            (DOMAIN_NOVEL,),
        ).fetchall()]
        if len(books) <= 1:
            return []
        owners: set[str] = set()
        for w in words:
            counts = {}
            for b in books:
                counts[b] = conn.execute(
                    "SELECT COUNT(*) AS c FROM knowledge_chunks "
                    "WHERE doc_name=? AND content LIKE ?", (b, f"%{w}%"),
                ).fetchone()["c"]
            total = sum(counts.values())
            if not total:
                continue
            top_book = max(counts, key=lambda k: counts[k])
            if counts[top_book] / total >= CLASS_WORD_DOMINANCE:
                owners.add(top_book)
        return sorted(owners)
    finally:
        conn.close()


FACT_SUBJECT_MIN_LEN = 2


def _fact_subject_hit(query: str) -> bool:
    """查询主体是否已被 facts 覆盖，且知识库里没有对应内容。

    两个条件都要满足才跳过：主体在 facts 里（说明已注入）+ 知识库块数极少
    （说明检索没东西可捞）。只满足前者时仍应检索——很多主体两边都有。
    """
    q = query or ""
    conn = connect()
    try:
        subjects = {r["subject"] for r in conn.execute(
            "SELECT DISTINCT subject FROM facts"
        ).fetchall() if len(r["subject"] or "") >= FACT_SUBJECT_MIN_LEN}
        for s in subjects:
            if s in q:
                n = conn.execute(
                    "SELECT COUNT(*) AS c FROM knowledge_chunks WHERE content LIKE ?",
                    (f"%{s}%",),
                ).fetchone()["c"]
                if n <= 2:  # 知识库基本没有 → 检索只会返回噪声
                    return True
        return False
    finally:
        conn.close()


def detect_domains(query: str) -> tuple[list[str], list[str]]:
    """查询 → (目标域, 目标文档名)。

    返回的文档名非空时进一步收窄到具体某本书——问《寂静杀戮》的角色不该
    捞到《食物链顶端的男人》（实测就发生了，4/6 命中来自另一本书）。
    两者都为空表示无法判定，调用方走全域。
    """
    q = query or ""
    # 小说：书名直接出现，或命中该书的专名（实体表 160 个）
    books: list[str] = []
    for book, names in _novel_names().items():
        short = book.replace("小说-", "").replace("小说－", "")
        if short and short in q:
            books.append(book)
            continue
        if any(n in q for n in names if len(n) >= 2):
            books.append(book)
    if books:
        return [DOMAIN_NOVEL], books

    # 人物名：定位到具体某本书（设定卡带 book 归属）
    for book, names in _novel_person_names().items():
        if any(n in q for n in names):
            return [DOMAIN_NOVEL], ([book] if book else [])

    # 小说体系类名（「命丛有哪些」——无专名但明确属于小说域）。
    # 进一步定位到书：命丛/命图这类体系词是《寂静杀戮》独有的，另一本没有。
    # 判据用实体表的书籍归属 + 该词在各书的出现频次，不硬编码书名。
    class_hit = [w for w in _novel_class_words() if w in q]
    if class_hit:
        return [DOMAIN_NOVEL], _books_for_class_words(class_hit)

    # facts 已覆盖的主体：明确跳过知识库检索（不是"判不出"）。
    # 实测「李羽的能力是什么」命中 6 块**全部无关**——因为李羽是用户自己在写
    # 的小说角色，设定存在 facts 表（8 条三元组，每次必注入），知识库里
    # 一个块都没有。知识库没内容时检索只会返回噪声，不如明确跳过。
    # 用 SKIP_SEARCH 哨兵与"判不出域"区分开：后者要走全域，前者不该检索。
    if _fact_subject_hit(q):
        return [SKIP_SEARCH], []

    # 小说通用词（没点明具体书）
    if re.search(r"小说|剧情|设定|主角|章节|人物|情节", q):
        return [DOMAIN_NOVEL], []

    if _RESUME_TERMS.search(q):
        return [DOMAIN_RESUME], []
    if _MANUAL_TERMS.search(q):
        return [DOMAIN_MANUAL], []
    if _PROJECT_TERMS.search(q):
        return [DOMAIN_PROJECT], []
    return [], []


# ── 回填与统计 ────────────────────────────────────────────

def backfill_domains() -> dict[str, int]:
    """给已有块打域标签（幂等：只填 domain='' 的行）。"""
    conn = connect()
    try:
        names = [r["doc_name"] for r in conn.execute(
            "SELECT DISTINCT doc_name FROM knowledge_chunks WHERE domain=''"
        ).fetchall()]
        counts: dict[str, int] = {}
        for name in names:
            domain = classify_doc(name)
            cur = conn.execute(
                "UPDATE knowledge_chunks SET domain=? WHERE doc_name=? AND domain=''",
                (domain, name),
            )
            counts[domain] = counts.get(domain, 0) + cur.rowcount
        conn.commit()
    finally:
        conn.close()
    if counts:
        logger.info("知识库分域回填: %s", counts)
    return counts


def domain_stats() -> dict[str, int]:
    conn = connect()
    try:
        return {r["domain"] or "(未分域)": r["n"] for r in conn.execute(
            "SELECT domain, COUNT(*) AS n FROM knowledge_chunks GROUP BY domain"
        ).fetchall()}
    finally:
        conn.close()
