"""MCP 身份上下文测试。"""
from types import SimpleNamespace

import pytest

from app.config import settings
from app.core.memory import owner_user_id
from app.mcp.context import McpContext, from_context


def test_stdio_owner_uses_configured_owner(monkeypatch):
    monkeypatch.setattr(settings, "mcp_stdio_role", "owner")
    monkeypatch.setattr(settings, "mcp_stdio_user_id", "")
    ctx = McpContext.stdio()
    assert ctx.uid == owner_user_id()
    assert ctx.role == "owner"
    assert ctx.is_owner is True


def test_owner_role_rejects_non_owner_uid(monkeypatch):
    monkeypatch.setattr(settings, "mcp_stdio_role", "owner")
    monkeypatch.setattr(settings, "mcp_stdio_user_id", "123456789012")
    if owner_user_id() == "123456789012":
        pytest.skip("测试 ID 恰好是主人 ID")
    with pytest.raises(ValueError, match="不能绑定非主人"):
        McpContext.stdio()


def test_sdk_context_keeps_request_metadata(monkeypatch):
    monkeypatch.setattr(settings, "mcp_stdio_role", "owner")
    monkeypatch.setattr(settings, "mcp_stdio_user_id", "")
    sdk_ctx = SimpleNamespace(
        request_id="req-1",
        request_context=SimpleNamespace(
            session=SimpleNamespace(
                client_params=SimpleNamespace(client_info=SimpleNamespace(name="test-host"))
            )
        ),
    )
    ctx = from_context(sdk_ctx)
    assert ctx.request_id == "req-1"
    assert ctx.client_name == "test-host"
    assert ctx.uid == owner_user_id()
