"""MCP 权限边界测试。"""
import pytest

from app.mcp.context import McpContext
from app.mcp.permissions import (
    McpPermissionError,
    require_confirmed_action,
    require_owner,
    require_read,
)


def test_read_only_allows_first_phase_roles():
    assert require_read(McpContext(uid="123", role="owner", is_owner=True)).role == "owner"
    assert require_read(McpContext(uid="123", role="internal", is_owner=False)).role == "internal"


@pytest.mark.parametrize("role", ["collector", "executor", "qq", "guest"])
def test_read_rejects_non_stdio_roles(role):
    with pytest.raises(McpPermissionError):
        require_read(McpContext(uid="123", role=role, is_owner=False))


def test_owner_only_rejects_non_owner():
    with pytest.raises(McpPermissionError):
        require_owner(McpContext(uid="123", role="owner", is_owner=False))


def test_confirmed_action_defaults_closed():
    ctx = McpContext(uid="123", role="owner", is_owner=True)
    with pytest.raises(McpPermissionError, match="需要显式确认"):
        require_confirmed_action(ctx)
