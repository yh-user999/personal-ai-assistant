"""第一阶段允许的低风险写入 MCP Tools。"""
from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.context import Context

from app.core import memory
from app.services import goals
from app.services.sanitize import sanitize

from ..audit import audited_tool
from ..permissions import require_owner
from ..schemas import cap_payload
from .common import text


@audited_tool
async def save_memory(
    content: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """保存一条普通记忆；入库前复用现有统一脱敏逻辑。"""
    identity = require_owner(ctx)
    original = text(content, "content")
    cleaned = sanitize(original)
    if not cleaned.strip():
        raise ValueError("content 脱敏后为空，未写入")
    memory_id = await memory.write_message("user", cleaned, user_id=identity.uid)
    return cap_payload({
        "saved": memory_id is not None,
        "memory_id": memory_id,
        "content_length": len(cleaned),
    })


@audited_tool
async def create_goal(
    title: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """创建一个普通目标；目标标题由现有服务统一脱敏并按 uid 隔离。"""
    identity = require_owner(ctx)
    value = text(title, "title", max_chars=200)
    goal_id = goals.add_goal(value, user_id=identity.uid)
    return cap_payload({"created": True, "goal_id": goal_id})
