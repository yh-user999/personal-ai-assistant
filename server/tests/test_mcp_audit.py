"""MCP 审计、脱敏与注册测试。"""
import json

import pytest

from app.config import settings
from app.core.memory import owner_user_id
from app.mcp.context import McpContext
from app.mcp.schemas import cap_payload, summarize_arguments
from app.mcp.server import create_server
from app.mcp.tools import write as write_tools
from app.models.database import connect


def owner_ctx() -> McpContext:
    return McpContext(uid=owner_user_id(), role="owner", is_owner=True)


@pytest.mark.asyncio
async def test_save_memory_sanitizes_and_audits(db, monkeypatch):
    captured = {}

    async def fake_write(sender, content, user_id=None):
        captured.update(sender=sender, content=content, user_id=user_id)
        return 42

    monkeypatch.setattr(write_tools.memory, "write_message", fake_write)
    monkeypatch.setattr(settings, "sensitive_terms", "机密词")
    result = await write_tools.save_memory("我的手机号 13812345678 和机密词", ctx=owner_ctx())
    assert result["saved"] is True
    assert captured["sender"] == "user"
    assert captured["user_id"] == owner_user_id()
    assert "13812345678" not in captured["content"]
    assert "机密词" not in captured["content"]

    row = connect().execute(
        "SELECT tool, user_id, success, arguments_summary FROM mcp_audit_logs "
        "WHERE tool='save_memory' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["user_id"] == owner_user_id()
    assert row["success"] == 1
    assert "13812345678" not in row["arguments_summary"]
    assert "机密词" not in row["arguments_summary"]
    assert json.loads(row["arguments_summary"])["content"] == {"length": len("我的手机号 13812345678 和机密词")}


def test_argument_summary_never_keeps_sensitive_text():
    summary = summarize_arguments({"query": "private text", "limit": 2})
    assert summary == {"query": {"length": 12}, "limit": 2}


def test_cap_payload_stays_within_budget():
    result = cap_payload({"results": [{"content": "x" * 1000} for _ in range(20)]}, max_chars=400)
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= 400
    assert result.get("truncated") is True


def test_server_registers_only_approved_capabilities(db, monkeypatch):
    monkeypatch.setattr(settings, "mcp_enabled", True)
    server = create_server()
    tools = {item.name for item in __import__("asyncio").run(server.list_tools())}
    resources = {item.uri for item in __import__("asyncio").run(server.list_resources())}
    prompts = {item.name for item in __import__("asyncio").run(server.list_prompts())}
    assert "execute_shell" not in tools
    assert "run_python" not in tools
    assert "delete_file" not in tools
    assert {"search_memories", "save_memory", "create_goal"} <= tools
    assert {
        "assistant://profile", "assistant://facts", "assistant://goals",
        "assistant://open-issues", "assistant://daily-summary",
    } <= resources
    assert {"review_novel_chapter", "summarize_week", "analyze_project_progress"} <= prompts
