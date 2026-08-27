"""知识库核心：文档入库（切块→向量化）与检索（KNN top-k）。

RAG 流水线：load → chunk → embed → store → search → generate。
本模块负责 chunk/embed/store/search 四步；generate 在 chat.py。
"""
import json
from datetime import datetime, timezone

from app.core import embedding
from app.core.chunker import chunk_text
from app.models.database import connect


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def ingest_document(name: str, content: str, replace: bool = True) -> dict:
    """文档入库：切块 → 批量向量化 → 存 knowledge_chunks + chunk_vectors。

    replace=True（默认）：同 doc_name 先删旧块再入库（同步更新不产生重复）。
    入库前统一脱敏（第 6.14 课）：敏感信息明文不进向量库。
    """
    from app.services.sanitize import sanitize

    name = sanitize(name)
    content = sanitize(content)
    chunks = chunk_text(content)
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

    # 批量向量化（一次 API 调用处理所有块）
    vectors = await embedding.embed(chunks)

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


def _bm25_rank(query: str, top_k: int = 10) -> list[dict]:
    """BM25 简化版：query 的字符 2-gram 在块中的出现次数加权（精确词敏感）。"""
    grams = [query[i:i + 2] for i in range(max(1, len(query) - 1))]
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, doc_name, chunk_index, content FROM knowledge_chunks"
        ).fetchall()
    finally:
        conn.close()
    scored = []
    for r in rows:
        score = sum(r["content"].count(g) for g in grams)
        if score > 0:
            scored.append((score, dict(r)))
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
    """知识库检索入口。method: hybrid（默认，向量+BM25 RRF）/ vector（纯向量）。"""
    if method == "vector":
        return await _vector_search(query, top_k)
    return await hybrid_search(query, top_k)


def format_knowledge_injection(hits: list[dict]) -> str:
    """检索结果注入 prompt 的格式（带来源标注，支持"引用"）。"""
    if not hits:
        return ""
    parts = []
    for i, h in enumerate(hits, 1):
        parts.append(
            f"[资料{i} · {h['doc_name']}#{h['chunk_index']} · 相关度{h['similarity']}]\n{h['content']}"
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
