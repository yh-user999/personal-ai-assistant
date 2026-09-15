"""小说词表与动态类名登记账本——knowledge_domain / novel_entities / 检索锚点共用。

此前三处各查各的表，且 knowledge_domain ↔ novel_entities 互相懒加载对方的
ENTITY_KINDS / _dynamic_novel_classes（隐藏循环依赖）。词表单一来源收口在本模块，
本模块不 import 这两个模块（依赖方向单向）。

- ENTITY_KINDS：实体类名触发词静态表（命丛/命图/功法/势力）
- 动态类名账本（dynamic_classes 表）：检索自愈兜底确认的体系词，带行数失效缓存
- novel_names / novel_class_words / novel_person_names：知识库与实体表的词表查询
- known_index_anchors：检索 query 扩展的全部锚点（书名+专名+类名+人名）
"""
import logging
import sqlite3

from app.models.database import connect

logger = logging.getLogger("assistant.novel_lexicon")

# 与 knowledge_domain.DOMAIN_NOVEL 同值；在此独立定义以保持依赖单向。
DOMAIN_NOVEL = "novel"

# ── 实体类型与它们的类名触发词 ──────────────────────────────
ENTITY_KINDS: dict[str, tuple[str, ...]] = {
    "命丛": ("命丛",),
    "命图": ("命图",),
    "功法": ("道术", "功法", "秘籍", "武功", "招式"),
    # 势力的触发词不能用单字「宗」「教」——会命中"宗旨""教训""教会"这类
    # 无关词，实测候选里混进了"孙悟空""恶意""封印"。用双字词组约束。
    "势力": ("门派", "兵团", "宗门", "教派", "帮派", "势力", "组织"),
}


# ── 动态类名词账本（检索自愈一期）──────────────────────────
# 缓存按表行数失效：登记新词 → 行数变 → 重建。
_dynamic_cache: dict[int, frozenset[str]] = {}


def dynamic_class_count() -> int:
    conn = connect()
    try:
        try:
            return conn.execute("SELECT COUNT(*) AS c FROM dynamic_classes").fetchone()["c"]
        except sqlite3.OperationalError:
            return 0
    finally:
        conn.close()


def dynamic_novel_classes() -> frozenset[str]:
    count = dynamic_class_count()
    cached = _dynamic_cache.get(count)
    if cached is not None:
        return cached
    conn = connect()
    try:
        try:
            words = frozenset(
                r["class_word"]
                for r in conn.execute(
                    "SELECT class_word FROM dynamic_classes WHERE domain='novel'"
                ).fetchall()
            )
        except sqlite3.OperationalError:
            words = frozenset()
    finally:
        conn.close()
    _dynamic_cache.clear()
    _dynamic_cache[count] = words
    return words


def register_class(class_word: str, domain: str = "", source_query: str = "") -> bool:
    """登记体系类名（幂等）。返回 True=新登记，False=已存在。

    domain='novel' 才参与域路由；不能确认领域归属时传 ''（只做登记防重复触发）。
    """
    from datetime import datetime, timezone

    word = (class_word or "").strip()
    if not word:
        return False
    conn = connect()
    try:
        exists = conn.execute(
            "SELECT 1 FROM dynamic_classes WHERE class_word=?", (word,)
        ).fetchone()
        if exists:
            return False
        conn.execute(
            """INSERT INTO dynamic_classes (class_word, domain, source_query, created_at)
               VALUES (?, ?, ?, ?)""",
            (word, domain, (source_query or "")[:200],
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    _dynamic_cache.clear()
    return True


def invalidate_dynamic_cache() -> None:
    """动态词表缓存失效（登记/纠错注销后调用）。"""
    _dynamic_cache.clear()


# ── 词表查询（知识库 + 实体表 + 设定卡）────────────────────

def novel_names() -> dict[str, set[str]]:
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


def novel_class_words() -> set[str]:
    """小说体系的类名（命丛/命图/道术…）——它们不是专名但同样能定位到小说域。

    「命丛有哪些」这种问法里没有任何专名，靠专名匹配判不出域，而这恰恰是
    污染最严重的问法（实测命中反代教程 PDF、AI 模板、名词焦虑 PDF）。
    检索自愈一期：动态登记的词（dynamic_classes 表 domain='novel'）并入，
    让"炼神"这类首问未覆盖、兜底后已确认的词第二次就能直接判域。
    """
    words = set(ENTITY_KINDS.keys())
    for group in ENTITY_KINDS.values():
        words.update(w for w in group if len(w) >= 2)
    words.update(dynamic_novel_classes())
    return words


def novel_person_names() -> dict[str, set[str]]:
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


def known_index_anchors() -> set[str]:
    """检索 query 扩展的全部锚点：书名（含去前缀短名）+ 专名 + 类名 + 人物名。

    词表故障时抛出的异常由调用方降级（退化为无锚点检索）。
    """
    anchors: set[str] = set()
    for book, names in novel_names().items():
        if book:
            anchors.add(book)
            anchors.add(book.replace("小说-", "").replace("小说－", ""))
        anchors.update(names)
    anchors.update(novel_class_words())
    for names in novel_person_names().values():
        anchors.update(names)
    return anchors
