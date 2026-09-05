"""MCP 工具共享辅助函数。"""
from __future__ import annotations

from typing import Any

from app.chat.retrieval import build_search_query

from ..context import McpContext, from_context
from ..schemas import bounded_limit, bounded_text, cap_payload


def get_context(value: Any | None = None) -> McpContext:
    return from_context(value)


def text(value: str, name: str, *, max_chars: int | None = None) -> str:
    return bounded_text(value, name=name, max_chars=max_chars)


def limit(value: int, name: str, *, default: int, maximum: int) -> int:
    return bounded_limit(value, name=name, default=default, maximum=maximum)


def build_query(
    original: str,
    history: list[dict[str, Any]],
    *,
    known_anchors: set[str] | None = None,
) -> tuple[str, list[str], bool]:
    return build_search_query(original, history, known_anchors=known_anchors or set())


def cap(value: Any) -> Any:
    return cap_payload(value)


def known_anchors(ctx: McpContext) -> set[str]:
    """只在主人/内部上下文中读取知识索引词，供追问 query 扩展。"""
    if not (ctx.is_owner or ctx.role.casefold() == "internal"):
        return set()
    anchors: set[str] = set()
    try:
        from app.services import knowledge_domain

        for book, names in knowledge_domain._novel_names().items():
            if book:
                anchors.add(book)
                anchors.add(book.replace("小说-", "").replace("小说－", ""))
            anchors.update(names)
        anchors.update(knowledge_domain._novel_class_words())
        for names in knowledge_domain._novel_person_names().values():
            anchors.update(names)
    except (ImportError, AttributeError, TypeError, ValueError):
        # 词表故障不影响正常原文查询，退化为无锚点检索。
        return set()
    return anchors
