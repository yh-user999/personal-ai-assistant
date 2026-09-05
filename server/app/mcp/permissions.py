"""MCP 工具的显式权限边界。"""
from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .context import McpContext, from_context

# 第一阶段仅本地 owner/internal stdio。远程、多用户及采集/执行角色后续再开放。
READ_ROLES = frozenset({"owner", "internal"})


class McpPermissionError(ToolError):
    """向 MCP Host 暴露的稳定权限错误，不泄露内部实现。"""

    code = "permission_denied"

    def __init__(self, message: str = "当前身份无权访问该私人数据") -> None:
        super().__init__(message)


def require_read(value: Any | None = None) -> McpContext:
    ctx = from_context(value)
    if ctx.role.casefold() not in READ_ROLES:
        raise McpPermissionError("当前 MCP 身份不允许读取私人数据")
    if not ctx.uid:
        raise McpPermissionError("MCP 身份缺少用户标识")
    return ctx


def require_owner(value: Any | None = None) -> McpContext:
    ctx = require_read(value)
    if not (ctx.is_owner or ctx.role.casefold() == "internal"):
        raise McpPermissionError("该能力仅限主人或内部服务使用")
    return ctx


def require_confirmed_action(value: Any | None = None, *, confirmed: bool = False) -> McpContext:
    ctx = require_owner(value)
    if not confirmed:
        raise McpPermissionError("该操作需要显式确认；第一阶段未开放未确认执行")
    return ctx
