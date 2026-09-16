"""应用组合根使用的端口集合。

端口协议定义在 ``app.contracts``；这里仅负责把当前进程中的 legacy
实现装配成应用层可消费的对象。MCP、HTTP 等适配层不应直接知道 core 路径。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.contracts.ports import KnowledgePort, LanguageModelPort, MemoryPort


@dataclass(frozen=True, slots=True)
class ApplicationPorts:
    """一组由组合根注入到应用用例的基础能力。"""

    memory: MemoryPort
    knowledge: KnowledgePort
    llm: LanguageModelPort


def get_default_ports() -> ApplicationPorts:
    """装配当前单进程默认实现；迁移期仍复用现有 core 模块。"""
    from app.core import knowledge as core_knowledge
    from app.core import llm as core_llm
    from app.core import memory as core_memory

    return ApplicationPorts(
        memory=core_memory,
        knowledge=core_knowledge,
        llm=core_llm,
    )


__all__ = ["ApplicationPorts", "get_default_ports"]
