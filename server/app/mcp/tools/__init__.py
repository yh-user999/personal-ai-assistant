"""MCP Tool 注册表。"""
from __future__ import annotations

from typing import Any

from .knowledge import search_knowledge, search_novel_entities
from .memory import get_recent_history, get_user_facts, search_memories
from .novel import (
    cancel_novel_job,
    get_novel_index_status,
    get_novel_job,
    list_novel_chapters,
    list_novel_projects,
    publish_novel_job,
    retry_novel_job,
    search_novel_chapters,
    sync_novel_file_index,
)
from .tasks import list_goals, list_open_issues
from .write import create_goal, save_memory

ALL_TOOLS = (
    search_memories,
    get_recent_history,
    get_user_facts,
    search_knowledge,
    search_novel_entities,
    list_goals,
    list_open_issues,
    list_novel_projects,
    list_novel_chapters,
    get_novel_job,
    publish_novel_job,
    retry_novel_job,
    cancel_novel_job,
    search_novel_chapters,
    sync_novel_file_index,
    get_novel_index_status,
    save_memory,
    create_goal,
)


def register_tools(server: Any) -> None:
    for tool in ALL_TOOLS:
        server.add_tool(tool, structured_output=True)


__all__ = [
    "ALL_TOOLS",
    "cancel_novel_job",
    "create_goal",
    "get_novel_index_status",
    "get_novel_job",
    "get_recent_history",
    "get_user_facts",
    "list_goals",
    "list_novel_chapters",
    "list_novel_projects",
    "list_open_issues",
    "publish_novel_job",
    "register_tools",
    "retry_novel_job",
    "save_memory",
    "search_knowledge",
    "search_memories",
    "search_novel_chapters",
    "search_novel_entities",
    "sync_novel_file_index",
]
