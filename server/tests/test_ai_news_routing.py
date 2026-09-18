"""每日 AI 资讯聊天快捷读取的作用域回归。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.chat import routing
from app.chat.context import ChatContext, ChatRequest


def _context(message: str, *, owner: bool, group_id: str = "") -> ChatContext:
    return ChatContext(
        request=type("Request", (), {"state": type("State", (), {})()})(),
        request_model=ChatRequest(message=message, group_id=group_id or None),
        message=message,
        uid="owner" if owner else "guest",
        is_owner=owner,
        group_id=group_id,
    )


def test_ai_news_command_is_registered_and_guest_blocked():
    assert any(name == "ai_news" for name, _ in routing._COMMAND_HANDLERS)
    assert "ai_news" in routing.GUEST_BLOCKED_HANDLERS


def test_owner_can_read_ai_news_without_llm():
    runtime = SimpleNamespace(
        services=SimpleNamespace(
            ai_news=SimpleNamespace(latest_digest=lambda uid: {"content": "# 今日 AI 资讯"}),
        ),
    )
    result = asyncio.run(routing._ai_news(_context("今日 AI 资讯", owner=True), runtime))
    assert result is not None
    assert result.reply == "# 今日 AI 资讯"


def test_guest_and_group_cannot_read_ai_news():
    runtime = SimpleNamespace(
        services=SimpleNamespace(
            ai_news=SimpleNamespace(latest_digest=lambda uid: {"content": "不应返回"}),
        ),
    )
    guest = asyncio.run(routing._ai_news(_context("今日 AI 资讯", owner=False), runtime))
    group = asyncio.run(routing._ai_news(_context("今日 AI 资讯", owner=True, group_id="123"), runtime))
    assert guest is None
    assert group is None
