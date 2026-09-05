"""MCP 请求上下文。

第一阶段只运行本地 stdio 进程，身份来自启动配置而不是工具参数；这样外部
模型不能通过传入 user_id 越权读取其他用户数据。"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from app.config import settings
from app.core.memory import normalize_user_id, owner_user_id


@dataclass(frozen=True, slots=True)
class McpContext:
    """经过本地配置确定的 MCP 身份与请求元数据。"""

    uid: str
    role: str = "owner"
    is_owner: bool = False
    request_id: str = ""
    client_name: str = ""

    @classmethod
    def stdio(cls) -> McpContext:
        role = (settings.mcp_stdio_role or "owner").strip().casefold()
        if role not in {"owner", "internal"}:
            raise ValueError("MCP stdio role 只能是 owner 或 internal")

        configured_uid = (settings.mcp_stdio_user_id or "").strip()
        uid = normalize_user_id(configured_uid or None)
        if role == "owner" and uid != owner_user_id():
            raise ValueError("MCP owner 角色不能绑定非主人 user_id")
        return cls(uid=uid, role=role, is_owner=(uid == owner_user_id()))

    def for_request(self, *, request_id: str = "", client_name: str = "") -> McpContext:
        return replace(self, request_id=request_id or "", client_name=client_name or "")


def _client_name(sdk_context: Any) -> str:
    try:
        params = sdk_context.request_context.session.client_params
        info = getattr(params, "client_info", None)
        return str(getattr(info, "name", "") or "")
    except (AttributeError, TypeError):
        return ""


def from_context(value: Any | None = None) -> McpContext:
    """把 MCP SDK Context 或测试用的 McpContext 统一成内部上下文。"""
    if isinstance(value, McpContext):
        return value
    base = McpContext.stdio()
    if value is None:
        return base
    try:
        request_id = str(getattr(value, "request_id", "") or "")
    except (AttributeError, TypeError):
        request_id = ""
    return base.for_request(request_id=request_id, client_name=_client_name(value))


def current_context() -> McpContext:
    """获取当前 stdio 身份；资源和提示词没有 SDK Context 注入时使用。"""
    return McpContext.stdio()
