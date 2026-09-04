"""私人 MCP Server：本地 stdio 入口与安全业务工具。"""
from __future__ import annotations

from typing import Any


def create_server() -> Any:
    """延迟导入 Server，避免仅使用上下文/工具时触发完整注册。"""
    from .server import create_server as _create_server

    return _create_server()


__all__ = ["create_server"]
