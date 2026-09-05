"""MCP 记忆工具测试。"""

import pytest

from app.config import settings
from app.mcp.context import McpContext
from app.mcp.permissions import McpPermissionError
from app.mcp.schemas import McpInputError
from app.mcp.tools import memory as memory_tools


def owner_ctx() -> McpContext:
    return McpContext(uid="owner", role="owner", is_owner=True)


@pytest.mark.asyncio
async def test_search_memories_preserves_original_and_expands_query(db, monkeypatch):
    captured = {}

    monkeypatch.setattr(
        memory_tools.memory,
        "get_recent_history",
        lambda limit, user_id=None: [{"role": "user", "content": "我们刚讨论夜海"}],
    )
    monkeypatch.setattr(memory_tools, "known_anchors", lambda ctx: {"夜海"})

    async def fake_search(query, top_k, min_similarity, user_id):
        captured.update(query=query, top_k=top_k, user_id=user_id)
        return [{"id": 1, "sender": "user", "content": "夜海", "score": 0.9}]

    monkeypatch.setattr(memory_tools.memory, "search", fake_search)
    result = await memory_tools.search_memories("再确认一下", top_k=100, ctx=owner_ctx())
    assert result["original_query"] == "再确认一下"
    assert "夜海" in result["search_query"]
    assert result["expanded"] is True
    assert captured == {"query": result["search_query"], "top_k": 20, "user_id": "owner"}
    assert result["results"][0]["id"] == 1


@pytest.mark.asyncio
async def test_memory_tool_rejects_oversized_query(db):
    with pytest.raises(McpInputError):
        await memory_tools.search_memories("x" * (settings.mcp_max_input_chars + 1), ctx=owner_ctx())


@pytest.mark.asyncio
async def test_get_recent_history_bounds_limit(db, monkeypatch):
    captured = {}

    def fake_history(limit, user_id=None):
        captured.update(limit=limit, user_id=user_id)
        return []

    monkeypatch.setattr(memory_tools.memory, "get_recent_history", fake_history)
    result = await memory_tools.get_recent_history(limit=999, ctx=owner_ctx())
    assert captured == {"limit": 20, "user_id": "owner"}
    assert result["limit"] == 20


@pytest.mark.asyncio
async def test_get_user_facts_requires_owner(db):
    with pytest.raises(McpPermissionError):
        await memory_tools.get_user_facts(ctx=McpContext(uid="123", role="owner", is_owner=False))
