"""知识库核心：文档入库（切块→向量化）与检索（KNN top-k）。

RAG 流水线：load → chunk → embed → store → search → generate。
本模块负责 chunk/embed/store/search 四步；generate 在 chat.py。
"""
import json
import logging
from datetime import datetime, timezone

from app.core import embedding

logger = logging.getLogger("assistant.knowledge")
from app.core.chunker import chunk_text
from app.models.database import connect


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 检索调参（都是实测定出来的，改前先看注释里的数据）──────────
# BM25 相对向量的权重。从 1.5 提到 3：BM25 只要 8~27ms 且专名精度远高
# （搜「银河灵潮」命中 1 块），向量要 218~1244ms 却区分不出相关与无关。
BM25_WEIGHT = 3.0
# 向量 top-k 的相似度极差下限。低于此值说明 embedding 各向异性导致向量
# 无区分力（实测所有块都塌在 0.023~0.025，极差 0.002），本轮放弃向量结果。
VECTOR_SPREAD_MIN = 0.005
# 分域过滤时向量候选的放大系数：vec0 虚表不支持 MATCH + WHERE，只能先多取再过滤
VECTOR_FILTER_FANOUT = 8
MAX_VECTOR_K = 200


# 人物别名（小说知识库策划数据：同一角色的多个名字/称呼，检索时多变体融合）
NOVEL_ALIASES = {
    "左志诚": ["左擎苍"],
    "左擎苍": ["左志诚"],
}


def _grams_text(text: str) -> str:
    """文本 → 2-gram 空格分隔串（知识库 FTS 索引与查询共用）。"""
    text = (text or "").strip()
    if len(text) < 2:
        return text
    return " ".join(
        g for g in (text[i:i + 2] for i in range(len(text) - 1))
        if _word_char(g[0]) and _word_char(g[1])
    )


def _fts_sync_doc(conn, name: str, chunks: list[str], chunk_ids: list[int]) -> None:
    """同步某文档全部 chunk 的 FTS 行（ingest 覆盖时先删后插）。"""
    conn.execute(
        "DELETE FROM knowledge_fts WHERE chunk_id IN "
        "(SELECT id FROM knowledge_chunks WHERE doc_name=?)",
        (name,),
    )
    for cid, chunk in zip(chunk_ids, chunks):
        conn.execute(
            "INSERT INTO knowledge_fts (chunk_id, grams) VALUES (?, ?)",
            (cid, _grams_text(chunk)),
        )


def _fts_backfill(conn) -> None:
    """存量 chunk 一次性回填 FTS（init_db 时调用，FTS 空而 chunks 非空才执行）。"""
    n_chunk = conn.execute("SELECT COUNT(*) AS n FROM knowledge_chunks").fetchone()["n"]
    n_fts = conn.execute("SELECT COUNT(*) AS n FROM knowledge_fts").fetchone()["n"]
    if n_chunk == 0 or n_fts > 0:
        return
    logger.info("知识库 FTS 回填：%d 块", n_chunk)
    ids, grams = [], []
    for r in conn.execute("SELECT id, content FROM knowledge_chunks").fetchall():
        ids.append(r["id"])
        grams.append(_grams_text(r["content"]))
    for cid, g in zip(ids, grams):
        conn.execute("INSERT INTO knowledge_fts (chunk_id, grams) VALUES (?, ?)", (cid, g))
    conn.commit()


async def ingest_document(
    name: str,
    content: str,
    replace: bool = True,
    sanitize_content: bool = True,
    chunk_size: int = 500,
    overlap: int = 50,
) -> dict:
    """文档入库：切块 → 分批向量化 → 存 knowledge_chunks + chunk_vectors。

    replace=True（默认）：同 doc_name 先删旧块再入库（同步更新不产生重复）。
    入库前统一脱敏（第 6.14 课）；sanitize_content=False 跳过脱敏——
    用于小说等长文本（避免形似手机号的数字串被误打码）。
    chunk_size/overlap：小说知识库建议 1500/150——一场戏完整落进一块。
    """
    if sanitize_content:
        from app.services.sanitize import sanitize

        name = sanitize(name)
        content = sanitize(content)
    chunks = chunk_text(content, chunk_size=chunk_size, overlap=overlap)
    if not chunks:
        return {"chunks": 0, "error": "文档为空或无可切分内容"}

    conn = connect()
    try:
        if replace:
            # 删除同文档旧块及其向量（sqlite-vec 虚拟表按 chunk_id 删除）
            old = conn.execute(
                "SELECT id FROM knowledge_chunks WHERE doc_name=?", (name,)
            ).fetchall()
            for r in old:
                conn.execute("DELETE FROM chunk_vectors WHERE chunk_id=?", (r["id"],))
                conn.execute("DELETE FROM knowledge_fts WHERE chunk_id=?", (r["id"],))
            conn.execute("DELETE FROM knowledge_chunks WHERE doc_name=?", (name,))
            conn.commit()
    finally:
        conn.close()

    # 分批向量化（长文档单批超 API 上限）
    vectors = await embedding.embed_batched(chunks)

    conn = connect()
    try:
        for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
            cur = conn.execute(
                """INSERT INTO knowledge_chunks (doc_name, chunk_index, content, created_at)
                   VALUES (?, ?, ?, ?)""",
                (name, i, chunk, _now()),
            )
            conn.execute(
                "INSERT INTO chunk_vectors (chunk_id, embedding) VALUES (?, ?)",
                (cur.lastrowid, json.dumps(vec)),
            )
            conn.execute(
                "INSERT INTO knowledge_fts (chunk_id, grams) VALUES (?, ?)",
                (cur.lastrowid, _grams_text(chunk)),
            )
        conn.commit()
    finally:
        conn.close()
    return {"chunks": len(chunks), "doc": name}


async def _vector_search(query: str, top_k: int = 3, *,
                         domains: list[str] | None = None,
                         docs: list[str] | None = None) -> list[dict]:
    """纯向量检索：问题向量化 → cosine KNN。

    domains/docs 是分域过滤。sqlite-vec 的 vec0 虚表**不支持在 MATCH 查询里
    附加 WHERE 条件**（会报 "A LIMIT or k = ? constraint is required"），
    所以只能先按 k 取更多候选再后过滤——过滤比例未知，放大系数取 8 倍。
    """
    qvec = (await embedding.embed([query]))[0]
    filtered = bool(domains or docs)
    k = min(top_k * VECTOR_FILTER_FANOUT, MAX_VECTOR_K) if filtered else top_k
    conn = connect()
    try:
        cur = conn.execute(
            """SELECT c.id, c.doc_name, c.chunk_index, c.content, c.domain, v.distance
               FROM chunk_vectors v
               JOIN knowledge_chunks c ON c.id = v.chunk_id
               WHERE v.embedding MATCH ? AND k = ?
               """,
            (json.dumps(qvec), k),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    if filtered:
        rows = [
            r for r in rows
            if (not docs or r["doc_name"] in docs)
            and (not domains or (r["domain"] or "") in domains)
        ][:top_k]

    results = []
    for r in rows:
        if r["distance"] is None:
            continue
        sim = max(0.0, 1.0 - float(r["distance"]))  # cosine 距离 → 相似度
        results.append({
            "id": r["id"],
            "doc_name": r["doc_name"],
            "chunk_index": r["chunk_index"],
            "content": r["content"],
            "similarity": round(sim, 3),
        })
    return results


def _word_char(ch: str) -> bool:
    return ch.isalnum() or "\u4e00" <= ch <= "\u9fff"


def _bm25_rank(query: str, top_k: int = 10, *,
               domains: list[str] | None = None,
               docs: list[str] | None = None) -> list[dict]:
    """BM25 排序：FTS5 倒排 + 内置 bm25()（v0.3.2 替代 Python 全表打分）。

    gram 化语义与旧实现一致；词频饱和由 FTS5 内置 bm25 等价承担。
    分域过滤直接下推到 SQL——FTS5 与普通表 JOIN 后可以正常加 WHERE。
    """
    grams = _grams_text(query).split()
    if not grams:
        return []
    match = " OR ".join(f'"{g}"' for g in grams)
    where = ["knowledge_fts MATCH ?"]
    args: list = [match]
    if docs:
        where.append(f"c.doc_name IN ({','.join('?' * len(docs))})")
        args.extend(docs)
    if domains:
        where.append(f"c.domain IN ({','.join('?' * len(domains))})")
        args.extend(domains)
    args.append(top_k)
    conn = connect()
    try:
        rows = conn.execute(
            f"""
            SELECT c.id, c.doc_name, c.chunk_index, c.content
            FROM knowledge_fts f JOIN knowledge_chunks c ON c.id = f.chunk_id
            WHERE {' AND '.join(where)}
            ORDER BY bm25(knowledge_fts) LIMIT ?
            """,
            args,
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("知识库 FTS 检索失败（退化为空候选）: %s", e)
        return []
    finally:
        conn.close()


async def hybrid_search(query: str, top_k: int = 3, *,
                        domains: list[str] | None = None,
                        docs: list[str] | None = None) -> list[dict]:
    """混合检索（默认）：RRF 融合向量语义 + BM25 关键词（k=60 经典参数）。

    评测结果：MRR 0.906 → 0.938，精确词问题（"几小时运行一次"）显著受益。
    domains/docs 为分域过滤（见 services/knowledge_domain）。
    """
    vec_hits = await _vector_search(query, top_k=10, domains=domains, docs=docs)
    bm25_hits = _bm25_rank(query, 10, domains=domains, docs=docs)

    # 向量无区分力检测：实测本地 embedding 各向异性严重——所有块的相似度都
    # 塌在 0.023~0.025（0.002 宽）。此时向量结果等于随机噪声，融进 RRF 只会
    # 把 BM25 的正确命中挤下去，不如整轮放弃向量。
    if len(vec_hits) >= 3:
        sims = [h.get("similarity", 0.0) for h in vec_hits]
        if max(sims) - min(sims) < VECTOR_SPREAD_MIN:
            logger.debug("向量相似度无区分力（极差 %.4f），本轮仅用 BM25",
                         max(sims) - min(sims))
            vec_hits = []

    rrf: dict[int, float] = {}
    for rank, h in enumerate(vec_hits, 1):
        rrf[h["id"]] = rrf.get(h["id"], 0) + 1 / (60 + rank)
    for rank, h in enumerate(bm25_hits, 1):
        # BM25 权重 3×：精确证据词（挖走/蜃宗）应压过"高频人名"的语义近邻。
        # 从 1.5 提到 3——实测 BM25 只要 8~27ms 且专名精度远高于向量
        # （向量 218~1244ms 却区分不出相关与无关）。
        rrf[h["id"]] = rrf.get(h["id"], 0) + BM25_WEIGHT / (60 + rank)
    ranked_raw = sorted(rrf.items(), key=lambda kv: -kv[1])[:top_k]
    # 并列分 tie-break：rrf 相同 → 向量相似度高者优先（语义更相关）→ id 兜底稳定
    by_id = {h["id"]: h for h in vec_hits + bm25_hits}
    ranked = sorted(
        ranked_raw,
        key=lambda kv: (
            -kv[1],
            -(by_id.get(kv[0]) or {}).get("similarity", 0.0),
            kv[0],
        ),
    )[:top_k]

    out = []
    for cid, score in ranked:
        h = by_id.get(cid)
        if not h:
            continue
        item = dict(h)
        item["rrf"] = round(score, 4)
        # 纯 BM25 命中的块没有向量相似度 → 用 rrf 充当展示分
        item["similarity"] = h.get("similarity", round(score, 3))
        out.append(item)
    return out


async def search_knowledge(query: str, top_k: int = 3, method: str = "hybrid") -> list[dict]:
    """知识库检索入口。method: hybrid（默认，向量+BM25 RRF）/ vector（纯向量）。

    支持人物别名多查询融合（小说知识库策划数据）：同一角色的多个名字
    （如 左志诚=左擎苍）各自检索后再按排名融合——跨名字指代的剧情问题
    才能命中"事件发生时的名字"所在场景。

    v0.4：embedding 调用加降级保护——向量服务故障时退化为关键词检索，
    不再让整个聊天请求 500（与记忆检索的降级策略一致）。
    """
    queries = [query]
    for alias, alts in NOVEL_ALIASES.items():
        if alias in query:
            for alt in alts:
                if alt not in query:
                    queries.append(query.replace(alias, alt))

    # 分域路由：先严格限定域（问小说只搜小说、能定位到书就只搜那本），
    # 无结果再全域兜底。实测混检时「李羽的能力是什么」命中 6 块全部无关。
    from app.services.knowledge_domain import SKIP_SEARCH, detect_domains

    domains, docs = detect_domains(query)
    if SKIP_SEARCH in domains:
        # 主体已被 facts 覆盖且知识库无对应内容——检索只会返回噪声
        logger.debug("查询主体已由 facts 覆盖，跳过知识库检索")
        return []
    try:
        if len(queries) == 1:
            if method == "vector":
                hits = await _vector_search(query, top_k, domains=domains, docs=docs)
            else:
                hits = await hybrid_search(query, top_k, domains=domains, docs=docs)
            if hits or not (domains or docs):
                return hits
            # 严格分域无结果 → 全域兜底（BM25 只要 20ms，多跑一次可以接受）
            logger.debug("分域 %s/%s 无结果，退回全域检索", domains, docs)
            if method == "vector":
                return await _vector_search(query, top_k)
            return await hybrid_search(query, top_k)

        # 多变体 RRF 融合：每个变体 top_k 个候选按排名加权合并
        by_id: dict[int, dict] = {}
        merged: dict[int, float] = {}
        async def _run_variants(dm: list[str] | None, dc: list[str] | None) -> list[dict]:
            by_id.clear()
            merged.clear()
            for q in queries:
                hits = (
                    await hybrid_search(q, top_k, domains=dm, docs=dc)
                    if method == "hybrid"
                    else await _vector_search(q, top_k, domains=dm, docs=dc)
                )
                for rank, h in enumerate(hits, 1):
                    by_id[h["id"]] = h
                    merged[h["id"]] = merged.get(h["id"], 0) + 1 / (60 + rank)
            ranked = sorted(merged.items(), key=lambda kv: -kv[1])[:top_k]
            return [{**by_id[cid], "rrf": round(score, 4)} for cid, score in ranked]

        out = await _run_variants(domains, docs)
        if not out and (domains or docs):
            logger.debug("分域 %s/%s 无结果（多变体），退回全域", domains, docs)
            out = await _run_variants(None, None)
        return out
    except Exception as e:
        logger.warning("知识库检索失败，退化为空候选: %s", e)
        return []


def expand_chunks(hits: list[dict], radius: int = 1, max_chars: int = 4000) -> list[dict]:
    """把首条命中扩展为连续剧情段（同文档邻域块拼接），其余命中保持单块。

    零成本提升小说问答的"情节完整性"。半径与上限按 1500 字/块调校：
    ±1 邻域 = 3 块 ≈ 4500 字的一整场戏，足够回答剧情因果类问题。
    落在扩展区间内的后续命中自动去重，避免重复注入。
    """
    if not hits:
        return []
    top = hits[0]
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT chunk_index, content FROM knowledge_chunks "
            "WHERE doc_name=? AND chunk_index BETWEEN ? AND ? ORDER BY chunk_index",
            (
                top["doc_name"],
                max(0, top["chunk_index"] - radius),
                top["chunk_index"] + radius,
            ),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return list(hits)
    lo, hi = rows[0]["chunk_index"], rows[-1]["chunk_index"]
    expanded_text = "\n".join(r["content"] for r in rows)[:max_chars]
    out = [{
        "doc_name": top["doc_name"],
        "chunk_index": f"{lo}-{hi}",
        "content": expanded_text,
        "similarity": top["similarity"],
        "expanded": True,
    }]
    for h in hits[1:]:
        if h["doc_name"] == top["doc_name"] and lo <= h["chunk_index"] <= hi:
            continue  # 已在扩展区间内，去重
        out.append(dict(h))
    return out


def get_alias_note(query: str) -> str:
    """人物别名提示：查询涉及别名时注入给 LLM（跨名字指代的理解前提）。"""
    notes = []
    for alias, alts in NOVEL_ALIASES.items():
        if alias in query:
            for alt in alts:
                notes.append(f"{alias}与{alt}是同一人物的两个名字")
    return "；".join(dict.fromkeys(notes))


def get_novel_facts(query: str) -> list[str]:
    """小说设定卡检索：query 命中关键词的权威事实条目（优先级最高）。"""
    conn = connect()
    try:
        rows = conn.execute("SELECT keywords, content FROM novel_facts").fetchall()
    finally:
        conn.close()
    matched = []
    for r in rows:
        for kw in r["keywords"].replace("，", ",").split(","):
            if kw.strip() and kw.strip() in query:
                matched.append(r["content"])
                break
    return matched


def format_knowledge_injection(hits: list[dict]) -> str:
    """检索结果注入 prompt 的格式（带来源标注，支持"引用"）。"""
    if not hits:
        return ""
    parts = []
    for i, h in enumerate(hits, 1):
        kind = "剧情片段" if h.get("expanded") else "片段"
        parts.append(
            f"[资料{i} · {h['doc_name']}#{h['chunk_index']} · {kind} · 相关度{h['similarity']}]\n{h['content']}"
        )
    return "\n\n".join(parts)


def list_documents() -> list[dict]:
    """知识库文档清单（文档名 + 块数）。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT doc_name, COUNT(*) AS n FROM knowledge_chunks GROUP BY doc_name ORDER BY doc_name"
        ).fetchall()
    finally:
        conn.close()
    return [{"doc": r["doc_name"], "chunks": r["n"]} for r in rows]
