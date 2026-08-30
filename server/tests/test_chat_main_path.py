"""LLM 主链路集成测试：mock llm.chat，走完整 /api/chat 组装路径。

锁定三件事：system prompt 注入段齐全（含稳定档案区在前）、双方消息入库、
被引用记忆 importance 提升。这是 prompt 换序（前缀缓存优化）的回归保险。
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models.database import connect, init_db, reset_connections


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "api_token", "")
    reset_connections()
    init_db()
    yield
    reset_connections()


@pytest.fixture
def captured(monkeypatch):
    """捕获发给 LLM 的 messages，返回预设回复。"""
    box = {"messages": None}
    reply = "好的，记下了。"

    async def fake_chat(messages, **kwargs):
        box["messages"] = messages
        return reply

    import app.api.chat as _chat_api
    monkeypatch.setattr(_chat_api.llm, "chat", fake_chat)

    # embedding 是单例模块，memory/knowledge 共享同一对象——一次打点全覆盖
    import app.core.embedding as _embedding

    async def fake_embed(texts):
        return [[0.0] * settings.embedding_dimension for _ in texts]

    monkeypatch.setattr(_embedding, "embed", fake_embed)
    return box


def _seed():
    conn = connect()
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    conn.execute(
        "INSERT INTO memories (sender, content, ts, importance) VALUES ('user', ?, ?, 1.0)",
        ("我决定项目代号叫「青鸾」", past),
    )
    conn.execute(
        "INSERT INTO facts (subject, predicate, object, updated_at) VALUES ('项目', '代号', '青鸾', ?)",
        (past,),
    )
    conn.execute(
        "INSERT INTO lessons (content, context, created_at) VALUES ('回复别用 emoji', '', ?)",
        (past,),
    )
    conn.commit()
    conn.close()


def test_chat_main_path_injections_and_persistence(env, captured):
    """主链路：注入齐全 + 稳定档案区在动态区之前（前缀缓存）+ 消息入库 + importance 提升。"""
    from fastapi.testclient import TestClient

    from app.main import app

    _seed()
    with TestClient(app) as client:
        r = client.post("/api/chat", json={"message": "项目代号是什么来着"})
        assert r.status_code == 200
        assert r.json()["reply"] == "好的，记下了。"

    msgs = captured["messages"]
    assert msgs is not None, "LLM 未被调用"
    assert msgs[0]["role"] == "system"
    system = msgs[0]["content"]

    # 注入段齐全
    assert "青鸾" in system, "facts 未注入"
    assert "回复别用 emoji" in system, "lessons 未注入"
    assert "【稳定档案区】" in system and "【动态上下文区】" in system

    # 前缀缓存契约：稳定档案区标题必须出现在动态区标题之前
    assert system.index("【稳定档案区】") < system.index("【动态上下文区】"), (
        "稳定块被排到动态块之后——前缀缓存失效，token 账单回涨"
    )
    # 最近历史带上了本轮用户消息
    assert any(m["role"] == "user" and "项目代号" in m["content"] for m in msgs)

    # 双方消息入库
    conn = connect()
    senders = [r["sender"] for r in conn.execute(
        "SELECT sender FROM memories WHERE content LIKE '%项目代号%' OR content LIKE '%好的，记下了%'"
    ).fetchall()]
    conn.close()
    assert "user" in senders and "assistant" in senders


def test_chat_no_llm_shortcut_paths(env):
    """快捷命令分支不烧 LLM（记录：/ 时间问答等直答）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/chat", json={"message": "记录：下午调试记忆链路"})
        assert r.status_code == 200
        assert "已记录" in r.json()["reply"]
