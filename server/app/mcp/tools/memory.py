"""私人记忆相关 MCP Tools。"""
from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.context import Context

from app.config import settings
from app.core import memory

from ..audit import audited_tool
from ..permissions import require_owner, require_read
from ..schemas import cap_payload
from .common import build_query, get_context, known_anchors, limit, text


def _public_memory(item: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "sender", "content", "summary", "ts", "topics", "score")
    return {key: item[key] for key in keys if key in item}


@audited_tool
async def search_memories(
    query: str,
    top_k: int = 5,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """检索当前 MCP 身份可见的个人记忆。"""
    identity = require_read(ctx)
    original = text(query, "query")
    k = limit(top_k, "top_k", default=5, maximum=20)
    history = memory.get_recent_history(min(8, settings.history_limit), user_id=identity.uid)
    search_query, anchors, expanded = build_query(
        original,
        history,
        known_anchors=known_anchors(identity),
    )
    hits = await memory.search(
        search_query,
        top_k=k,
        min_similarity=settings.min_similarity,
        user_id=identity.uid,
    )
    return cap_payload({
        "original_query": original,
        "search_query": search_query,
        "anchors": anchors,
        "expanded": expanded,
        "results": [_public_memory(item) for item in hits],
    })


@audited_tool
async def get_recent_history(
    limit: int = 8,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """读取当前 MCP 身份最近的对话历史。"""
    identity = require_read(ctx)
    from ..schemas import bounded_limit

    n = bounded_limit(limit, name="limit", default=8, maximum=20)
    rows = memory.get_recent_history(n, user_id=identity.uid)
    return cap_payload({"limit": n, "results": rows})


@audited_tool
async def get_user_facts(ctx: Context | None = None) -> dict[str, Any]:
    """读取主人/内部服务可见的持久事实注入文本。"""
    identity = require_owner(ctx)
    facts = memory.get_facts_injection(user_id=identity.uid)
    return cap_payload({
        "user_id": identity.uid,
        "facts_text": facts,
        "has_facts": bool(facts),
    })
