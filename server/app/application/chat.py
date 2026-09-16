"""聊天应用用例：把 HTTP/MCP 之外的聊天编排依赖集中成显式入口。"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.chat.context import ChatContext, ChatRequest, ChatResponse, ChatRuntime, build_context
from app.chat.pipeline import run_chat, run_chat_stream
from app.chat.prompting import _GENERATION_INTENT, SYSTEM_PROMPT, _untrusted_reference
from app.chat.routing import _COMMAND_HANDLERS, GUEST_BLOCKED_HANDLERS, parse_time_question
from app.contracts.ports import KnowledgePort, LanguageModelPort, MemoryPort

# 旧 API/外部调用方的兼容导出；真实实现仍属于 chat 子层。
__all__ = [
    "ChatApplication",
    "_COMMAND_HANDLERS",
    "_GENERATION_INTENT",
    "GUEST_BLOCKED_HANDLERS",
    "SYSTEM_PROMPT",
    "_untrusted_reference",
    "parse_time_question",
]


@dataclass(slots=True)
class ChatApplication:
    """聊天用例门面；不处理 HTTP 鉴权、幂等、上传或 SSE 编码。"""

    settings: Any
    llm: LanguageModelPort
    memory: MemoryPort
    knowledge: KnowledgePort
    services_factory: Callable[..., Any]
    bg_tasks: set[asyncio.Task]
    logger: Any

    def build_context(self, request_model: ChatRequest, request: Any) -> ChatContext:
        """将传输层请求转换为聊天流水线上下文。"""
        return build_context(request_model, request, self.memory)

    def build_runtime(self, request: Any | None = None) -> ChatRuntime:
        """按当前组合根创建一轮聊天所需的运行时依赖。"""
        return ChatRuntime(
            settings=self.settings,
            llm=self.llm,
            memory=self.memory,
            knowledge=self.knowledge,
            services=self.services_factory(),
            bg_tasks=self.bg_tasks,
            logger=self.logger,
        )

    async def run(self, request_model: ChatRequest, request: Any) -> ChatResponse:
        """执行完整聊天用例；请求幂等由 HTTP adapter 在外层负责。"""
        context = self.build_context(request_model, request)
        return await run_chat(context, self.build_runtime(request))

    async def run_stream(
        self,
        context: ChatContext,
        on_delta: Callable[[str], Any],
        runtime: ChatRuntime | None = None,
    ) -> ChatResponse:
        """执行流式聊天用例；SSE 帧编码由 HTTP adapter 负责。"""
        return await run_chat_stream(
            context,
            runtime or self.build_runtime(context.request),
            on_delta,
        )
