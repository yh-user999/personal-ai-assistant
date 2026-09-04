"""MCP 目标和未解决问题工具测试。"""
import pytest

from app.mcp.context import McpContext
from app.mcp.schemas import McpInputError
from app.mcp.tools import tasks as task_tools


def owner_ctx() -> McpContext:
    return McpContext(uid="owner", role="owner", is_owner=True)


@pytest.mark.asyncio
async def test_list_goals_passes_uid_and_status(db, monkeypatch):
    captured = {}

    def fake_list(user_id, status=None, limit=10):
        captured.update(user_id=user_id, status=status, limit=limit)
        return [{"id": 1, "title": "测试", "status": "active"}]

    monkeypatch.setattr(task_tools.goals, "list_goals", fake_list)
    result = await task_tools.list_goals(status="ACTIVE", limit=999, ctx=owner_ctx())
    assert captured == {"user_id": "owner", "status": "active", "limit": 50}
    assert result["results"][0]["title"] == "测试"


@pytest.mark.asyncio
async def test_list_open_issues_passes_uid(db, monkeypatch):
    captured = {}

    def fake_list(user_id, limit=20):
        captured.update(user_id=user_id, limit=limit)
        return [{"id": 2, "topic": "阻塞", "status": "open"}]

    monkeypatch.setattr(task_tools.unresolved, "list_open_issues", fake_list)
    result = await task_tools.list_open_issues(limit=3, ctx=owner_ctx())
    assert captured == {"user_id": "owner", "limit": 3}
    assert result["results"][0]["status"] == "open"


@pytest.mark.asyncio
async def test_task_limit_rejects_non_integer(db):
    with pytest.raises(McpInputError):
        await task_tools.list_goals(limit="many", ctx=owner_ctx())
