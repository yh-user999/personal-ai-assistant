"""聊天应用组合根：集中装配 legacy 依赖，不反向依赖 HTTP API。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.application.chat import ChatApplication
from app.application.ports import get_default_ports
from app.chat.services_registry import make_services
from app.config import settings


_DEFAULT_BG_TASKS: set[asyncio.Task] = set()


def get_chat_application(
    *,
    bg_tasks: set[asyncio.Task] | None = None,
    logger: Any | None = None,
) -> ChatApplication:
    """创建默认聊天应用；服务模块仍在每轮 runtime 创建时装配。"""
    ports = get_default_ports()
    return ChatApplication(
        settings=settings,
        llm=ports.llm,
        memory=ports.memory,
        knowledge=ports.knowledge,
        services_factory=make_services,
        bg_tasks=bg_tasks if bg_tasks is not None else _DEFAULT_BG_TASKS,
        logger=logger or logging.getLogger("assistant.chat"),
    )
