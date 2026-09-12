"""只收录端点测试：入库到群作用域、不调 LLM、不产生回复、拒绝私聊滥用。"""
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models.database import connect, init_db, reset_connections


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "group_profile_enabled", False)  # 隔离画像提取
    reset_connections()
    init_db()

    import app.core.embedding as _embedding

    async def fake_embed(texts):
        return [[0.0] * settings.embedding_dimension for _ in texts]

    monkeypatch.setattr(_embedding, "embed", fake_embed)
    yield
    reset_connections()


def test_observe_stores_message_into_group_scope(db_env):
    with TestClient(app) as client:
        r = client.post("/api/chat/observe", json={
            "message": "群里有人聊到了新的显卡", "user_id": "10086", "group_id": "916000001",
        })
    assert r.status_code == 200
    assert r.json()["stored"] is True

    conn = connect()
    try:
        row = conn.execute(
            "SELECT user_id, group_id, sender, content FROM memories"
        ).fetchone()
    finally:
        conn.close()
    assert row["group_id"] == "916000001"
    assert row["user_id"] == "10086"
    assert row["sender"] == "user"
    assert "显卡" in row["content"]


def test_observe_never_calls_llm(db_env, monkeypatch):
    """只收录必须零 LLM 调用：否则这些群会产生成本且可能生成回复。"""
    called = {"n": 0}

    async def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("只收录端点不得调用 LLM")

    from app.core import llm

    monkeypatch.setattr(llm, "chat", boom, raising=False)
    monkeypatch.setattr(llm, "chat_json", boom, raising=False)

    with TestClient(app) as client:
        r = client.post("/api/chat/observe", json={
            "message": "一句普通的群聊内容", "user_id": "10086", "group_id": "916000001",
        })
    assert r.status_code == 200
    assert called["n"] == 0


def test_observe_returns_no_reply_field(db_env):
    """响应里不得有 reply：调用方不应把它当聊天接口用。"""
    with TestClient(app) as client:
        payload = client.post("/api/chat/observe", json={
            "message": "群聊内容", "user_id": "10086", "group_id": "916000001",
        }).json()
    assert "reply" not in payload


def test_observe_rejects_private_scope(db_env):
    """缺少 group_id 必须拒绝：防止绕过聊天链路往私聊记忆写数据。"""
    with TestClient(app) as client:
        r = client.post("/api/chat/observe", json={
            "message": "试图写进私聊", "user_id": "10086",
        })
    assert r.status_code == 400

    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    finally:
        conn.close()


def test_observe_skips_empty_message(db_env):
    with TestClient(app) as client:
        r = client.post("/api/chat/observe", json={
            "message": "   ", "user_id": "10086", "group_id": "916000001",
        })
    assert r.status_code == 200 and r.json()["stored"] is False


def test_observed_messages_are_isolated_from_private(db_env):
    """收录的群消息不得被私聊检索命中。"""
    import asyncio

    from app.core import memory as memory_module

    with TestClient(app) as client:
        client.post("/api/chat/observe", json={
            "message": "群里讨论的机械键盘话题", "user_id": "10086", "group_id": "916000001",
        })

    private = asyncio.run(memory_module.search("机械键盘", user_id="10086"))
    assert all("机械键盘" not in m["content"] for m in private), "私聊不得检索到群消息"

    grouped = asyncio.run(
        memory_module.search("机械键盘", user_id="10086", group_id="916000001")
    )
    assert any("机械键盘" in m["content"] for m in grouped), "本群应可检索"
