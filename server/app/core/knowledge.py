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


async def ingest_document(name: str, content: str) -> dict:
    """文档入库：切块 → 批量向量化 → 存 knowledge_chunks + chunk_vectors。"""
    chunks = chunk_text(content)
    if not chunks:
        return {"chunks": 0, "error": "文档为空或无可切分内容"}

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


async def search_knowledge(query: str, top_k: int = 3) -> list[dict]:
    """知识库检索：问题向量化 → cosine KNN → 返回最相关块（含文档名/相似度）。"""
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
