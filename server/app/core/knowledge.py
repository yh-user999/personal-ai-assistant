"""知识库核心：文档入库（切块→向量化）与检索（KNN top-k）。

RAG 流水线：load → chunk → embed → store → search → generate。
本模块负责 chunk/embed/store/search 四步；generate 在 chat.py。
"""
import json
import math
from datetime import datetime, timezone

from app.core import embedding
from app.core.chunker import chunk_text
from app.models.database import connect


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# 人物别名（小说知识库策划数据：同一角色的多个名字/称呼，检索时多变体融合）
NOVEL_ALIASES = {
    "左志诚": ["左擎苍"],
    "左擎苍": ["左志诚"],
}


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
        conn.commit()
    finally:
        conn.close()
    return {"chunks": len(chunks), "doc": name}


async def _vector_search(query: str, top_k: int = 3) -> list[dict]:
    """纯向量检索：问题向量化 → cosine KNN。"""
    qvec = (await embedding.embed([query]))[0]
    conn = connect()
    try:
        cur = conn.execute(
            """SELECT c.id, c.doc_name, c.chunk_index, c.content, v.distance
               FROM chunk_vectors v
               JOIN knowledge_chunks c ON c.id = v.chunk_id
               WHERE v.embedding MATCH ? AND k = ?
               """,
            (json.dumps(qvec), top_k),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

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


def _bm25_rank(query: str, top_k: int = 10) -> list[dict]:
    """BM25（字符 2-gram + IDF + 词频饱和 + 长度归一，k1=1.5，b=0.75）。

    词频饱和是关键：名词解释章里"命丛"出现 50 次只按 ~2.4 次计分，
    不再线性霸榜；"挖走"这类稀有证据词得以浮出水面。
    标点脏 gram（"丛，"等）过滤掉——它们只会帮倒忙。
    """
    grams = list(dict.fromkeys(
        q for q in (query[i:i + 2] for i in range(max(1, len(query) - 1)))
        if _word_char(q[0]) and _word_char(q[1])
    ))
    if not grams:
        return []
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, doc_name, chunk_index, content FROM knowledge_chunks"
        ).fetchall()
    finally:
        conn.close()
    docs = [dict(r) for r in rows]
    n = max(1, len(docs))
    lengths = [len(d["content"]) for d in docs]
    avgdl = sum(lengths) / n if lengths else 1.0
    df = {g: 0 for g in grams}
    for d in docs:
        for g in grams:
            if g in d["content"]:
                df[g] += 1
    k1, b = 1.5, 0.75

    def _idf(g: str) -> float:
        c = df[g]
        return math.log(1 + (n - c + 0.5) / (c + 0.5)) if c else 0.0

    scored = []
    for d, L in zip(docs, lengths):
        s = 0.0
        for g in grams:
            tf = d["content"].count(g)
            if tf:
                norm = tf * (k1 + 1) / (tf + k1 * (1 - b + b * L / avgdl))
                s += norm * _idf(g)
        if s > 0:
            scored.append((s, d))
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:top_k]]


async def hybrid_search(query: str, top_k: int = 3) -> list[dict]:
    """混合检索（默认）：RRF 融合向量语义 + BM25 关键词（k=60 经典参数）。

    评测结果：MRR 0.906 → 0.938，精确词问题（"几小时运行一次"）显著受益。
    """
    vec_hits = await _vector_search(query, top_k=10)
    bm25_hits = _bm25_rank(query, 10)
    rrf: dict[int, float] = {}
    for rank, h in enumerate(vec_hits, 1):
        rrf[h["id"]] = rrf.get(h["id"], 0) + 1 / (60 + rank)
    for rank, h in enumerate(bm25_hits, 1):
        rrf[h["id"]] = rrf.get(h["id"], 0) + 1 / (60 + rank)
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
    """
    queries = [query]
    for alias, alts in NOVEL_ALIASES.items():
        if alias in query:
            for alt in alts:
                if alt not in query:
                    queries.append(query.replace(alias, alt))
    if len(queries) == 1:
        if method == "vector":
            return await _vector_search(query, top_k)
        return await hybrid_search(query, top_k)

    # 多变体 RRF 融合：每个变体 top_k 个候选按排名加权合并
    by_id: dict[int, dict] = {}
    merged: dict[int, float] = {}
    for q in queries:
        hits = (
            await hybrid_search(q, top_k)
            if method == "hybrid"
            else await _vector_search(q, top_k)
        )
        for rank, h in enumerate(hits, 1):
            by_id[h["id"]] = h
            merged[h["id"]] = merged.get(h["id"], 0) + 1 / (60 + rank)
    ranked = sorted(merged.items(), key=lambda kv: -kv[1])[:top_k]
    out = []
    for cid, score in ranked:
        item = dict(by_id[cid])
        item["rrf"] = round(score, 4)
        out.append(item)
    return out


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
