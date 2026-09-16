"""应用层使用的最小端口协议。

协议只描述调用方需要的能力，不绑定 SQLite、FastAPI 或具体 LLM SDK；现有
legacy 模块仍可通过结构化类型直接作为实现传入。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol


class MemoryPort(Protocol):
    async def write_message(
        self,
        sender: str,
        content: str,
        user_id: str | None = None,
        precomputed_vec: list[float] | None = None,
        group_id: str | None = None,
    ) -> int | None: ...

    async def search(
        self,
        query: str,
        top_k: int = 8,
        min_similarity: float = 0.35,
        user_id: str | None = None,
        group_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def get_recent_history(
        self,
        limit: int = 8,
        user_id: str | None = None,
        group_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def get_facts_injection(
        self,
        limit: int = 40,
        user_id: str | None = None,
    ) -> str: ...


class KnowledgePort(Protocol):
    async def search_knowledge(
        self,
        query: str,
        top_k: int = 3,
        method: str = "hybrid",
    ) -> list[dict[str, Any]]: ...

    def last_vector_degraded(self) -> bool: ...

    def expand_chunks(
        self,
        hits: list[dict[str, Any]],
        radius: int = 1,
        max_chars: int = 1500,
    ) -> list[dict[str, Any]]: ...

    def format_knowledge_injection(self, hits: list[dict[str, Any]]) -> str: ...

    def get_alias_note(self, query: str) -> str: ...

    def get_novel_facts(self, query: str) -> str: ...


class LanguageModelPort(Protocol):
    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> str: ...

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncIterator[str]: ...
