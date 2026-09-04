"""目标与未解决问题 MCP Tools。"""
from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.context import Context

from app.services import goals, unresolved

from ..audit import audited_tool
from ..permissions import require_read
from ..schemas import bounded_limit, cap_payload


@audited_tool
async def list_goals(
    status: str | None = None,
    limit: int = 10,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """读取当前 MCP 身份的目标列表。"""
    identity = require_read(ctx)
    status_value = (status or "").strip().casefold() or None
    if status_value not in {None, "active", "done", "paused"}:
        raise ValueError("status 只能是 active、done 或 paused")
    n = bounded_limit(limit, name="limit", default=10, maximum=50)
    rows = goals.list_goals(identity.uid, status=status_value, limit=n)
    return cap_payload({"status": status_value, "limit": n, "results": rows})


@audited_tool
async def list_open_issues(
    limit: int = 20,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """读取当前 MCP 身份仍处于 open 状态的问题。"""
    identity = require_read(ctx)
    n = bounded_limit(limit, name="limit", default=20, maximum=50)
    rows = unresolved.list_open_issues(identity.uid, limit=n)
    return cap_payload({"limit": n, "results": rows})
