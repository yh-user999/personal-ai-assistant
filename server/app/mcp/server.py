"""私人 MCP Server 的本地 stdio 入口。"""
from __future__ import annotations

import logging
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

from app.config import settings
from app.models.database import init_db

from .prompts import register_prompts
from .resources import register_resources
from .tools import register_tools

logger = logging.getLogger("assistant.mcp")


SERVER_NAME = "personal-ai-assistant"
SERVER_VERSION = "0.1.0"


def create_server() -> MCPServer:
    """初始化数据库并创建未启动的 MCP Server。"""
    if not settings.mcp_enabled:
        raise RuntimeError("MCP 已关闭，请设置 MCP_ENABLED=true 后再启动")
    init_db()
    server = MCPServer(
        name=SERVER_NAME,
        title="Personal AI Assistant",
        description="私人记忆、知识库、目标与项目分析工具",
        instructions="只访问当前本地 MCP 身份允许的数据；写入工具仅用于普通记忆和目标。",
        version=SERVER_VERSION,
        log_level="WARNING",
    )
    register_tools(server)
    register_resources(server)
    register_prompts(server)
    return server


def main() -> int:
    """启动 stdio 传输；普通日志固定输出 stderr。"""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    try:
        server = create_server()
    except Exception as exc:
        logger.error("MCP Server 启动失败: %s", exc)
        return 1
    logger.info("MCP stdio Server 已启动")
    server.run("stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover - 由 stdio Host 驱动
    raise SystemExit(main())
