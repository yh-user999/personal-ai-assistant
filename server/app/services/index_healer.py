"""检索自愈（一期）：索引未覆盖的枚举式提问，自动兜底回答 + 登记类名。

立项案例：「炼神里面有哪些境界」——"炼神"不在任何词表里，判不出域被
静默全域兜底；书里写作"练神"（错别字变体），核心词没命中任何块，
LLM 诚实地说"没有记载"，但书里其实有内容。

一期只做三件事（见 docs/检索自愈与答案自检方案.md）：
1. 检测器：枚举句式 +（判不出域 或 核心词未命中）→ 触发
2. 兜底：候选词变体重搜（炼神→练神）→ 聚合 top N 块 → LLM 提炼带出处
3. 登记：聚合块 ≥60% 来自小说域时，把类名登记进动态词表（第二次秒答）
二期（后台自动实体抽取）与三期（反馈修正）不在本模块。
"""
import logging
import re
from datetime import datetime, timezone

from app.core import knowledge, llm
from app.models.database import connect

logger = logging.getLogger("assistant.healer")

# ── 触发信号 ────────────────────────────────────────────────

# 枚举句式：问"清单/阶梯/体系"的问法。与 novel_entities.detect_enum_intent
# 的语义一致，但这里更宽——不只小说类名，任何未被覆盖的体系词都算。
_ENUM_RE = re.compile(
    r"有哪些|哪几个|分为|分几|划分|共几|几个|几层|几重|几境|"
    r"什么境界|哪些境界|体系|等级|层次|境界"
)

# 候选类名词抽取（两条贪心模式）：
# A：名词 + 里/里面/中… + 枚举词 —— "炼神里面有哪些境界" → 炼神
# B：名词（可含和/与链）+ 枚举词 —— "武道境界和修道境界分别有哪几个"
#    → 整段"武道境界和修道境界分别"，切分后把"分别"等语气词滤掉。
_CAND_RE_A = re.compile(
    r"([\u4e00-\u9fff和与]{2,12})"
    r"(?:里|里面|中|之中|内|内部)"
    r"(?:有哪|哪些|分为|分几|几(?:个|种|层|重|境)|什么境界|哪些境界|境界|体系|等级|层次)"
)
_CAND_RE_B = re.compile(
    r"([\u4e00-\u9fff和与]{2,12})"
    r"(?:有哪|哪些|分为|分几|几(?:个|种|层|重|境)|什么境界|哪些境界)"
)

# 候选词再净化：去掉枚举句式词本身与常见无意义词
_STOP_WORDS = {"什么", "哪些", "境界", "体系", "等级", "层次", "里面", "一个",
               "几个", "分别", "这些", "那些", "到底"}

# 尾部语气/枚举词剥离："命丛有"→"命丛"、"修道境界分别"→"修道境界"、
# "命图分为"→"命图"（贪心捕获会把枚举词吞进候选里）
_FILLER_TAIL = re.compile(r"(?:分别|分为|有哪|哪些|有|的)+$")

def extract_candidates(query: str) -> list[str]:
    """从枚举问句里抽候选类名词。

    A 模式（名词+里/中+枚举词）命中时只信 A——它天然切割出精确名词段；
    A 无命中才用 B（名词+枚举词），并把"分别/有/的"等尾部助词剥掉。
    """
    q = query or ""
    raw: set[str] = set()
    a_hits: list[str] = []
    for m in _CAND_RE_A.finditer(q):
        a_hits.extend(p for p in re.split(r"[和与、，,/\s]+", m.group(1)))
    if a_hits:
        raw.update(a_hits)
    else:
        for m in _CAND_RE_B.finditer(q):
            seg = _FILLER_TAIL.sub("", m.group(1))
            raw.update(p for p in re.split(r"[和与、，,/\s]+", seg))
    out = []
    for w in sorted(raw, key=len, reverse=True):
        w = _FILLER_TAIL.sub("", w).strip()
        if 2 <= len(w) <= 8 and w not in _STOP_WORDS:
            out.append(w)
    return out

# 错别字/异写变体：书里常见混用（炼/练、丛/从、苍/仓、志/智…）。
# 每字最多 1 个替换目标，防变体爆炸。
_CONFUSABLE = {
    "炼": ["练"], "练": ["炼"],
    "丛": ["从"], "从": ["丛"],
    "苍": ["仓"], "仓": ["苍"],
    "志": ["智"], "智": ["志"],
    "启": ["起"], "起": ["启"],
}

# 聚合检索参数
AGGREGATE_TOP = 20        # 聚合块上限
# 境界类候选的聚合种子词：阶梯词常与类名相隔较远（"先练气，后练神，再练虚"），
# 只按候选词字面搜会漏掉阶梯段落——补搜这些通用境界词（无命中无害）。
_REALM_SEEDS = ("练气", "练神", "练虚", "显圣", "造化", "心神")
MIN_AGGREGATE_CHUNKS = 3  # 至少找到几块才值得提炼（1-2 块孤证直接放弃）
NOVEL_DOMINANCE = 0.6     # 聚合块来自小说域的比例 ≥ 此值才登记为 novel 域
SPARSE_WORD_CHUNKS = 3    # 命中块里词面出现不足此数 = 拼不出清单，仍走聚合
SYNTH_MAX_TOKENS = 900

def detect_enum_intent(query: str) -> bool:
    return bool(_ENUM_RE.search(query or ""))

def expand_variants(word: str) -> list[str]:
    """错别字/异写变体（含原词，每个变体每字只换一次，≤4 个）。"""
    variants = {word}
    for i, ch in enumerate(word):
        for alt in _CONFUSABLE.get(ch, []):
            variants.add(word[:i] + alt + word[i + 1:])
        if len(variants) >= 4:
            break
    return list(variants)[:4]

def core_word_missing(words: list[str], chunks: list[dict]) -> bool:
    """核心词自检：任一候选词出现在命中块内容里吗？全都没出现 = 检索没捞对。"""
    if not words:
        return False
    text = "\n".join((c.get("content") or "") for c in chunks)
    return not any(w in text for w in words)

def aggregate_chunks(variants: list[str], limit: int = AGGREGATE_TOP) -> list[dict]:
    """按变体词 FTS 捞块（优先词面命中，多词去重，按 chunk 顺序排）。"""
    seen: dict[int, dict] = {}
    for v in variants:
        try:
            hits = knowledge._bm25_rank(v, top_k=10)
        except Exception as e:
            logger.warning("[healer] 变体检索失败 %s: %s", v, e)
            continue
        for h in hits:
            if v in (h.get("content") or "") and h["id"] not in seen:
                seen[h["id"]] = h
            if len(seen) >= limit:
                break
        if len(seen) >= limit:
            break
    return sorted(seen.values(), key=lambda h: (h.get("doc_name") or "", h.get("chunk_index") or 0))

SYNTH_SYSTEM = (
    "你是小说设定提炼器。只输出针对用户问题的提炼结果："
    "原文有依据的内容写清楚并注明章节；原文没有的明确说「原文未见」；"
    "绝不编造，绝不复述或重复原文片段。"
)

SYNTH_PROMPT = """用户问题：{query}

原文片段：
{chunks}
"""

# 回显防护：模型把原文块标头吐出来 = 提炼退化（曾实测整段重复回显）
_ECHO_RE = re.compile(r"\[(?:小说|第)[^\]\n]{0,20}#?\d*\]")


async def synthesize(query: str, words: list[str], chunks: list[dict]) -> str:
    """聚合块 → LLM 提炼（一次调用，temperature 0）。失败/回显返回空串。"""
    lines = []
    for c in chunks:
        body = (c.get("content") or "").replace("\n", " ")[:250]
        lines.append(f"[{c.get('doc_name')}#{c.get('chunk_index')}] {body}")
    prompt = SYNTH_PROMPT.replace("{query}", query).replace(
        "{chunks}", "\n\n".join(lines)
    )
    try:
        # system 只给角色指令，正文放 user——纯 system 长文会让部分模型退化回显
        text = await llm.chat(
            [
                {"role": "system", "content": SYNTH_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=SYNTH_MAX_TOKENS,
        )
        text = (text or "").strip()
        if _ECHO_RE.search(text):
            logger.warning("[healer] 提炼疑似回显原文，弃用")
            return ""
        return text
    except Exception as e:
        logger.warning("[healer] 聚合提炼失败: %s", e)
        return ""


def _already_covered_words() -> frozenset[str]:
    """静态词表已覆盖的词（实体索引/书名）——这些词有专门路径，自愈不抢活。"""
    from app.services.novel_entities import ENTITY_KINDS
    from app.services.knowledge_domain import _novel_names

    words = set(ENTITY_KINDS.keys())
    for group in ENTITY_KINDS.values():
        words.update(w for w in group if len(w) >= 2)
    for book in _novel_names():
        words.add(book.replace("小说-", "").replace("小说－", ""))
    return frozenset(words)


def diagnose(query: str, domains: list[str], docs: list[str],
             hits: list[dict]) -> dict | None:
    """检测器：枚举句式 +（判不出域 或 核心词未命中）→ 返回触发信息。"""
    if not detect_enum_intent(query):
        return None
    covered_words = _already_covered_words()
    words = [w for w in extract_candidates(query) if w not in covered_words]
    if not words:
        return None
    unrouted = not domains and not docs
    missing = core_word_missing(words, hits)
    # 枚举式提问的第三触发：词面虽命中但块数太少（<3）——散在叙事里的清单
    # 靠 1-2 块拼不出来，仍需聚合提炼（"炼神"登记后词面命中 2 块正是此例）
    hit_count = sum(
        1 for c in hits if any(w in (c.get("content") or "") for w in words)
    )
    sparse = hit_count < SPARSE_WORD_CHUNKS
    if not unrouted and not missing and not sparse:
        return None
    return {
        "action": "heal",
        "words": words,
        "unrouted": unrouted,
        "core_missing": missing,
        "sparse": sparse,
    }


async def heal(diag: dict, query: str) -> tuple[str, list[dict]]:
    """兜底执行：变体重搜 → 聚合 → 提炼。返回 (注入文本, 聚合块)。"""
    chunks: list[dict] = []
    for w in diag["words"]:
        variants = expand_variants(w)
        # 境界类问题补种子词（练气/练神/练虚…），把阶梯段落也捞进聚合
        if any("境界" in w or w in ("炼神", "练神", "练虚") for w in diag["words"]):
            variants += [s for s in _REALM_SEEDS if s not in variants]
        chunks = aggregate_chunks(variants)
        if len(chunks) >= MIN_AGGREGATE_CHUNKS:
            break
    if len(chunks) < MIN_AGGREGATE_CHUNKS:
        return "", []
    text = await synthesize(query, diag["words"], chunks)
    if not text:
        return "", chunks
    note = (
        "【知识库聚合资料】以下内容由系统从原文相关片段聚合提炼，"
        "是可信的参考资料，回答时优先采用，可直接引用其中的章节号。"
        "若你此前回答过「没有记载/查不到」，以此为准主动更正，"
        "并自然地说一句「刚翻了原文，补充一下」：\n"
    )
    return note + text, chunks


def classify_aggregate_domain(chunks: list[dict]) -> str:
    """聚合块来源判定：≥60% 来自小说域文档 → 'novel'，否则 ''（只登记不路由）。"""
    if not chunks:
        return ""
    novel = sum(
        1 for c in chunks if ((c.get("domain") or "") == "novel"
                              or (c.get("doc_name") or "").startswith("小说"))
    )
    return "novel" if novel / len(chunks) >= NOVEL_DOMINANCE else ""

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
"""检索自愈二期/三期补充：后台自动实体抽取 + 用户纠错反馈回路。

二期：heal 兜底成功 → 后台自动抽取该体系词的专名实体（预算/幂等/置信三闸），
高置信直接入库，低置信进候选池等主人确认。
三期：用户说"不对，XX 不是境界"→ 把 XX 从动态词表/实体索引/候选池移除。
"""
# ── 二期：自动抽取 ─────────────────────────────────────────

AUTO_DAILY_LIMIT = 3   # 预算闸：每天最多自动抽取 3 次
AUTO_MAX_BLOCKS = 10   # 成本闸：自动抽取最多读 10 个候选块（手动模式 40）
AUTO_MIN_EVIDENCE = 2  # 置信闸：名字在 ≥2 块出现才直接入库，1 块进候选池

_CORRECTION_RE = re.compile(
    r"(?:不对|不是|说错了|搞错了|记错了)[，,：:\s]*"
    r"([\u4e00-\u9fffA-Za-z]{2,12}?)(?:不是|不算|不属于)[，,：:\s]*"
    r"([\u4e00-\u9fffA-Za-z]{2,12})"
)


def majority_novel_book(chunks: list[dict]) -> str:
    """聚合块中出现最多的那本小说（无小说块返回空串）。"""
    from collections import Counter

    books = Counter(
        c.get("doc_name") or ""
        for c in chunks
        if (c.get("doc_name") or "").startswith("小说")
    )
    return books.most_common(1)[0][0] if books else ""


def auto_budget_ok(kind_word: str) -> bool:
    """预算闸（每天 ≤AUTO_DAILY_LIMIT）+ 幂等闸（同一词当天只抽一次）。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = connect()
    try:
        n_today = conn.execute(
            "SELECT COUNT(*) AS c FROM auto_extract_log WHERE substr(extracted_at,1,10)=?",
            (today,),
        ).fetchone()["c"]
        dup = conn.execute(
            "SELECT 1 FROM auto_extract_log WHERE kind_word=? AND substr(extracted_at,1,10)=?",
            (kind_word, today),
        ).fetchone()
    finally:
        conn.close()
    if dup or n_today >= AUTO_DAILY_LIMIT:
        return False
    return True


def _log_auto_extract(kind_word: str, book: str, names_count: int) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO auto_extract_log (kind_word, book, extracted_at, names_count) "
            "VALUES (?, ?, ?, ?)",
            (kind_word, book, datetime.now(timezone.utc).isoformat(), names_count),
        )
        conn.commit()
    finally:
        conn.close()


def _chunk_evidence(book: str, name: str) -> int:
    """名字在书中出现的块数（零 LLM 的置信依据）。"""
    conn = connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM knowledge_chunks WHERE doc_name=? AND content LIKE ?",
            (book, f"%{name}%"),
        ).fetchone()["c"]
    finally:
        conn.close()


async def auto_extract_task(words: list[str], book: str) -> dict | None:
    """后台自动抽取入口（chat heal 成功后触发，fire-and-forget）。

    流程：预算/幂等闸 → extract_entities(dry_run) → 按块证据分置信
    → 高置信直接入库 / 低置信进候选池 → 台账留痕。全程失败不影响主回复。
    """
    if not words or not book:
        return None
    kind_word = words[0]
    if not auto_budget_ok(kind_word):
        return {"kind": kind_word, "skipped": "budget_or_duplicate"}

    from app.services import novel_entities

    try:
        payload = await novel_entities.extract_entities(
            book, kind_word, dry_run=True, max_blocks=AUTO_MAX_BLOCKS
        )
    except Exception as e:
        return {"kind": kind_word, "skipped": f"extract_error: {e}"}

    names = payload.get("names") or []
    confirmed: list[dict] = []
    for item in names:
        name = item.get("name")
        if not name:
            continue
        evidence = _chunk_evidence(book, name)
        if evidence >= AUTO_MIN_EVIDENCE:
            confirmed.append({"name": name, "first_chunk": item.get("first_chunk")})
        else:
            candidate_add(book, kind_word, name, item.get("first_chunk"))

    if confirmed:
        novel_entities.confirm_extracted(
            {
                "book": book,
                "kind": kind_word,
                "names": confirmed,
                "group_name": payload.get("group_name") or "",
                "group_size": payload.get("group_size") or 0,
            }
        )
    _log_auto_extract(kind_word, book, len(confirmed))
    return {
        "kind": kind_word,
        "book": book,
        "confirmed": len(confirmed),
        "candidates": len(names) - len(confirmed),
    }


# ── 候选池（低置信抽取）────────────────────────────────────

def candidate_add(book: str, kind: str, name: str, first_chunk) -> int:
    conn = connect()
    try:
        cur = conn.execute(
            """INSERT INTO entity_candidates (book, kind, name, first_chunk, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(book, kind, name) DO NOTHING""",
            (book, kind, name, first_chunk, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def candidate_list(limit: int = 10) -> list[dict]:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM entity_candidates WHERE status='pending' ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()]
    finally:
        conn.close()


def candidate_confirm(name: str) -> int:
    """候选转正：写进实体索引（verified=1）并标记候选状态。

    注意分段取连：upsert_entity 内部会复用并关闭线程缓存连接，
    若外层还握着同一连接继续用会 ProgrammingError（实测踩过）。
    """
    from app.services.novel_entities import upsert_entity

    conn = connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM entity_candidates WHERE name=? AND status='pending'", (name,)
        ).fetchall()]
    finally:
        conn.close()
    if not rows:
        return 0
    for r in rows:
        upsert_entity(r["book"], r["name"], r["kind"],
                      first_chunk=r["first_chunk"], verified=1)
    conn = connect()
    try:
        for r in rows:
            conn.execute(
                "UPDATE entity_candidates SET status='confirmed' WHERE id=?", (r["id"],)
            )
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def candidate_discard(name: str) -> int:
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE entity_candidates SET status='discarded' WHERE name=? AND status='pending'",
            (name,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ── 三期：用户纠错反馈回路 ─────────────────────────────────

def apply_correction(msg: str) -> str | None:
    """「不对，XX 不是 YY」→ 把 XX 从索引移除。命中返回回复文案，未命中 None。

    覆盖三处索引：动态词表（注销）、实体索引（删除）、候选池（废弃）。
    全部留痕 index_corrections，可审计可回滚。
    """
    m = _CORRECTION_RE.search(msg or "")
    if not m:
        return None
    target, negated = m.group(1), m.group(2)
    from app.services import knowledge_domain

    actions: list[str] = []
    conn = connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM dynamic_classes WHERE class_word=?", (target,)
        ).fetchone()
        if row:
            conn.execute("DELETE FROM dynamic_classes WHERE class_word=?", (target,))
            actions.append(f"把「{target}」移出体系词表")
        n = conn.execute(
            "DELETE FROM novel_entities WHERE name=?", (target,)
        ).rowcount
        if n:
            actions.append(f"删除实体索引里的「{target}」{n} 条")
        n2 = conn.execute(
            "UPDATE entity_candidates SET status='discarded' "
            "WHERE name=? AND status='pending'",
            (target,),
        ).rowcount
        if n2:
            actions.append(f"废弃候选「{target}」{n2} 条")
        if not actions:
            return None
        conn.execute(
            "INSERT INTO index_corrections (target, reason, corrected_at) VALUES (?, ?, ?)",
            (target, f"用户说它不是{negated}", datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    knowledge_domain.invalidate_dynamic_cache()
    return (
        "🤝 已修正：" + "；".join(actions)
        + f"。以后不会再把它当「{negated}」了。"
    )
