"""模块化单体迁移前的外部契约基线。

这些断言只锁定现有对外形状和安全边界，不把契约实现搬到新层，也不改变
运行时注册表；后续迁移必须先通过本文件和既有回归测试。
"""
from types import SimpleNamespace

import pytest

from app.chat.context import ChatRequest, ChatResponse
from app.chat.followup import FOLLOWUP_KINDS, build_interaction_hint
from app.main import AuthMiddleware, app
from app.mcp.tools import ALL_TOOLS, register_tools


CRITICAL_HTTP_METHODS = {
    "/api/chat": {"post"},
    "/api/chat/vision": {"post"},
    "/api/chat/stream": {"post"},
    "/api/chat/observe": {"post"},
    "/api/greeting": {"get"},
    "/api/messages": {"get"},
    "/api/messages/search": {"get"},
    "/api/health": {"get"},
    "/api/ready": {"get"},
}


def _api_paths() -> dict[str, dict]:
    return {
        path: methods
        for path, methods in app.openapi()["paths"].items()
        if path.startswith("/api/")
    }


def test_critical_http_routes_keep_methods():
    paths = _api_paths()
    for path, expected_methods in CRITICAL_HTTP_METHODS.items():
        assert path in paths, f"关键 HTTP 路由丢失：{path}"
        assert expected_methods <= set(paths[path]), path


def test_every_protected_api_path_has_an_auth_rule():
    paths = _api_paths()
    public_paths = {"/api/health", "/api/ready"}
    prefixes = tuple(prefix for prefix, _roles in AuthMiddleware.ROLE_RULES)

    for path in paths:
        if path in public_paths:
            continue
        assert any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes), path


def test_chat_models_keep_external_fields_and_defaults():
    assert set(ChatRequest.model_fields) == {
        "message",
        "request_id",
        "user_id",
        "group_id",
        "group_directed",
        "image",
    }
    assert set(ChatResponse.model_fields) == {"reply", "memories_used", "interaction"}
    assert ChatResponse(reply="ok", memories_used=0).model_dump() == {
        "reply": "ok",
        "memories_used": 0,
        "interaction": {},
    }


def test_group_interaction_contract_is_bounded():
    hint = build_interaction_hint(
        SimpleNamespace(is_group=True, message="你觉得这本小说怎么样"),
        SimpleNamespace(needs_clarification=False, social_action="answer"),
        "把书名或链接发我，我再按可靠内容聊。",
    )

    assert set(hint) == {"followup"}
    followup = hint["followup"]
    assert set(followup) == {"kind", "expires_in", "max_messages"}
    assert followup["kind"] in FOLLOWUP_KINDS
    assert followup["expires_in"] == 90
    assert followup["max_messages"] == 1


class _ToolRecorder:
    def __init__(self):
        self.calls = []

    def add_tool(self, tool, *, structured_output):
        self.calls.append((tool, structured_output))


def test_mcp_tool_registry_is_unique_and_structured():
    recorder = _ToolRecorder()
    register_tools(recorder)

    names = [tool.__name__ for tool, _structured in recorder.calls]
    assert len(recorder.calls) == len(ALL_TOOLS)
    assert len(names) == len(set(names))
    assert all(structured for _tool, structured in recorder.calls)
    assert {"search_memories", "search_knowledge", "save_memory"} <= set(names)


def test_mcp_disabled_still_rejects_startup(monkeypatch):
    from app.mcp import server as mcp_server
    from app.config import settings

    monkeypatch.setattr(settings, "mcp_enabled", False)
    with pytest.raises(RuntimeError, match="MCP 已关闭"):
        mcp_server.create_server()
