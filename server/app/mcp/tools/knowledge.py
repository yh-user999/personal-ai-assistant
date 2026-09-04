"""知识库与小说实体 MCP Tools。"""
from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.context import Context

from app.chat.retrieval import build_search_query
from app.core import knowledge, memory
from app.services import knowledge_domain, novel_entities

from ..audit import audited_tool
from ..permissions import require_read
from ..schemas import cap_payload
from .common import get_context, known_anchors, limit as bounded_count, text


def _public_hit(item: dict[str, Any]) -> dict[str, Any]:
    keys = ("doc_name", "chunk_index", "content", "similarity", "rrf", "expanded")
    return {key: item[key] for key in keys if key in item}


@audited_tool
async def search_knowledge(
    query: str,
    top_k: int = 5,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """按当前知识域检索知识库，并返回来源与 query 路由信息。"""
    identity = require_read(ctx)
    original = text(query, "query")
    k = bounded_count(top_k, "top_k", default=5, maximum=20)
    history = memory.get_recent_history(8, user_id=identity.uid)
    search_query, anchors, expanded = build_search_query(
        original,
        history,
        known_anchors=known_anchors(get_context(ctx)),
    )
    domains, docs = knowledge_domain.detect_domains(search_query)
    hits = await knowledge.search_knowledge(search_query, top_k=k)
    hits = knowledge.expand_chunks(hits, radius=1, max_chars=4000)
    degraded = bool(getattr(knowledge, "_vector_degraded_last", None) and knowledge._vector_degraded_last.get())
    return cap_payload({
        "original_query": original,
        "search_query": search_query,
        "anchors": anchors,
        "expanded": expanded,
        "routing": {"domains": domains, "documents": docs, "method": "hybrid"},
        "degraded": degraded,
        "results": [_public_hit(item) for item in hits[:k]],
    })


@audited_tool
async def search_novel_entities(
    query: str,
    entity_kind: str | None = None,
    book: str | None = None,
    limit: int = 50,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """检索已登记的小说实体，不触发 LLM 抽取。"""
    require_read(ctx)
    term = text(query, "query")
    n = bounded_count(limit, "limit", default=50, maximum=50)
    kind = (entity_kind or "").strip()[:40]
    book_name = (book or "").strip()[:160]
    rows = novel_entities.search_entities(term, entity_kind=kind or None, book=book_name or None, limit=n)
    return cap_payload({
        "query": term,
        "entity_kind": kind or None,
        "book": book_name or None,
        "limit": n,
        "results": rows,
    })
