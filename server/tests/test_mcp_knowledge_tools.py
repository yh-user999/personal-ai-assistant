"""MCP 知识和实体工具测试。"""
import pytest

from app.mcp.context import McpContext
from app.mcp.tools import knowledge as knowledge_tools


def owner_ctx() -> McpContext:
    return McpContext(uid="owner", role="owner", is_owner=True)


@pytest.mark.asyncio
async def test_search_knowledge_returns_routing_and_sources(db, monkeypatch):
    monkeypatch.setattr(
        knowledge_tools.memory,
        "get_recent_history",
        lambda limit, user_id=None: [],
    )
    monkeypatch.setattr(knowledge_tools, "known_anchors", lambda ctx: {"RAG"})
    monkeypatch.setattr(
        knowledge_tools.knowledge_domain,
        "detect_domains",
        lambda query: (["project_doc"], ["OPS"]),
    )

    async def fake_search(query, top_k):
        assert query == "RAG"
        assert top_k == 20
        return [{"id": 1, "doc_name": "OPS", "chunk_index": 2, "content": "资料", "similarity": 0.8}]

    monkeypatch.setattr(knowledge_tools.knowledge, "search_knowledge", fake_search)
    monkeypatch.setattr(knowledge_tools.knowledge, "expand_chunks", lambda hits, radius, max_chars: hits)
    result = await knowledge_tools.search_knowledge("RAG", top_k=999, ctx=owner_ctx())
    assert result["routing"] == {"domains": ["project_doc"], "documents": ["OPS"], "method": "hybrid"}
    assert result["results"][0]["doc_name"] == "OPS"


@pytest.mark.asyncio
async def test_search_novel_entities_applies_filters(db, monkeypatch):
    captured = {}

    def fake_search(query, entity_kind=None, book=None, limit=50):
        captured.update(query=query, entity_kind=entity_kind, book=book, limit=limit)
        return [{"name": "夜海", "kind": "命丛", "book": "小说-测试", "verified": 1}]

    monkeypatch.setattr(knowledge_tools.novel_entities, "search_entities", fake_search)
    result = await knowledge_tools.search_novel_entities(
        "夜", entity_kind="命丛", book="小说-测试", limit=999, ctx=owner_ctx()
    )
    assert captured == {"query": "夜", "entity_kind": "命丛", "book": "小说-测试", "limit": 50}
    assert result["results"][0]["name"] == "夜海"
