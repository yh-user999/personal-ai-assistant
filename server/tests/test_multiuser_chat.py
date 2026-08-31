"""Phase 2 多人聊天回归：访客模式（隔离上下文/限流/命令门禁/知识库隔离）。

背景（v0.4 多人支持 Phase 2）：
- /api/chat 接受 user_id（QQ 号）；空 = 主人，非法 400
- 访客：完全隔离的记忆/事实/目标；知识库/行为/情绪/教训不注入；
  主人专属命令族跳过；滑动窗口限流；消息长度上限 2000
- 主人路径行为不变（含前缀缓存契约）
"""
import asyncio
import time
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
    box = {"messages": []}
    reply = "好的，记下了。"

    async def fake_chat(messages, **kwargs):
        box["messages"].append(messages)
        return reply

    import app.api.chat as _chat_api
    monkeypatch.setattr(_chat_api.llm, "chat", fake_chat)

    import app.core.embedding as _embedding

    async def fake_embed(texts):
        return [[0.0] * settings.embedding_dimension for _ in texts]

    monkeypatch.setattr(_embedding, "embed", fake_embed)
    return box


def _seed_owner_data():
    """造主人数据：记忆 + 事实 + 教训 + 知识库。"""
    conn = connect()
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    conn.execute(
        "INSERT INTO memories (user_id, sender, content, ts, importance) VALUES ('owner', 'user', ?, ?, 1.0)",
        ("我决定项目代号叫「青鸾」", past),
    )
    conn.execute(
        "INSERT INTO facts (user_id, subject, predicate, object, updated_at) VALUES ('owner', '项目', '代号', '青鸾', ?)",
        (past,),
    )
    conn.execute(
        "INSERT INTO lessons (content, context, created_at) VALUES ('回复别用 emoji', '', ?)",
        (past,),
    )
    conn.execute(
        "INSERT INTO knowledge_chunks (doc_name, chunk_index, content, created_at) VALUES ('反代教程', 0, '反向代理配置教程内容', ?)",
        (past,),
    )
    conn.commit()
    conn.close()


def _post(client, message, user_id=None):
    payload = {"message": message}
    if user_id is not None:
        payload["user_id"] = user_id
    return client.post("/api/chat", json=payload)


def test_guest_chat_isolated_context(env, captured):
    """访客链路：隔离上下文（无主人事实/教训/知识库）+ 记忆按 QQ 号入库。"""
    from fastapi.testclient import TestClient

    from app.main import app

    _seed_owner_data()
    with TestClient(app) as client:
        r = _post(client, "今天天气怎么样", user_id="10002")
        assert r.status_code == 200
        assert r.json()["reply"] == "好的，记下了。"

    msgs = captured["messages"]
    assert len(msgs) == 1
    system = msgs[0][0]["content"]

    # 访客边界声明注入
    assert "QQ 用户 10002（访客" in system
    assert "功能对你不可用" in system
    # 主人数据零注入
    assert "青鸾" not in system, "主人的事实泄漏给访客"
    assert "回复别用 emoji" not in system, "主人的教训泄漏给访客"
    assert "反向代理配置教程内容" not in system, "知识库泄漏给访客"

    # 双方消息按访客 QQ 号入库
    conn = connect()
    rows = conn.execute(
        "SELECT user_id FROM memories WHERE content LIKE '%今天天气怎么样%' OR content LIKE '%好的，记下了%'"
    ).fetchall()
    conn.close()
    assert rows and all(r["user_id"] == "10002" for r in rows)


def test_owner_path_unchanged(env, captured):
    """主人链路：无 guest_note，注入照旧（前缀缓存契约不破）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    _seed_owner_data()
    with TestClient(app) as client:
        r = _post(client, "项目代号是什么来着")
        assert r.status_code == 200
    system = captured["messages"][0][0]["content"]
    assert "青鸾" in system
    assert "访客" not in system
    assert "【稳定档案区】" in system and system.index("【稳定档案区】") < system.index("【动态上下文区】")


def test_guest_rate_limit(env, captured):
    """访客限流：窗口内第 11 条被拒，不烧 LLM。"""
    from fastapi.testclient import TestClient

    from app.main import app

    import app.api.chat as _chat_api
    _chat_api._guest_events.clear()  # 测试间清状态

    with TestClient(app) as client:
        for i in range(10):
            r = _post(client, f"消息{i}", user_id="10003")
            assert r.status_code == 200
        r = _post(client, "第11条", user_id="10003")
        assert r.status_code == 200
        assert "聊得太快" in r.json()["reply"]
    assert len(captured["messages"]) == 10, "超限请求不应烧 LLM"


def test_guest_blocked_commands(env, captured):
    """访客的主人专属命令：不执行，落 LLM 路径（有访客边界提示）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = _post(client, "记录：测试工作日志", user_id="10002")
        assert r.status_code == 200
    # worklog 未写入
    conn = connect()
    n = conn.execute("SELECT COUNT(*) AS c FROM work_log").fetchone()["c"]
    conn.close()
    assert n == 0, "访客消息不应写入工作日志"
    # 但走了 LLM 主路径（回复正常）
    assert captured["messages"], "访客消息应落入 LLM 路径"


def test_guest_executor_blocked(env):
    """访客执行器命令：不产生任何 executor 指令。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = _post(client, "打开 chrome", user_id="10002")
        assert r.status_code == 200
    conn = connect()
    n = conn.execute("SELECT COUNT(*) AS c FROM executor_commands").fetchone()["c"]
    conn.close()
    assert n == 0


def test_invalid_user_id_400(env):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        assert _post(client, "你好", user_id="abc").status_code == 400
        assert _post(client, "你好", user_id="1234567890123").status_code == 400


def test_guest_msg_length_cap(env, captured):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = _post(client, "长" * 2001, user_id="10002")
        assert r.status_code == 200
        assert "太长" in r.json()["reply"]
    assert not captured["messages"], "超长消息不应烧 LLM"


def test_guest_goals_isolated(env):
    """访客目标命令：写进自己的目标桶，主人查不到。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = _post(client, "目标：学会Python", user_id="10002")
        assert "目标已记录" in r.json()["reply"]
    conn = connect()
    rows = conn.execute("SELECT user_id FROM goals WHERE title='学会Python'").fetchall()
    conn.close()
    assert rows and rows[0]["user_id"] == "10002"


def test_panel_messages_owner_only(env):
    """桌面面板 /messages：只返回主人消息（访客消息不混入）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        _post(client, "访客的悄悄话", user_id="10002")
        _post(client, "主人的消息")
        r = client.get("/api/messages?limit=200")
        assert r.status_code == 200
        contents = [m["content"] for m in r.json()["messages"]]
        assert "访客的悄悄话" not in contents
        assert "主人的消息" in contents


def test_guest_search_scoped(env):
    """访客「搜索聊天记录」只命中自己的。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        _post(client, "主人的服务器密码相关讨论", user_id=None)
        _post(client, "访客的独特关键词甲", user_id="10002")
        r = _post(client, "搜索聊天记录：独特关键词甲", user_id="10002")
        assert "独特关键词甲" in r.json()["reply"]
        r2 = _post(client, "搜索聊天记录：独特关键词甲", user_id=None)
        assert "没有在聊天记录里找到" in r2.json()["reply"]


def test_rate_limit_owner_exempt(env, captured):
    """主人不限流。"""
    from fastapi.testclient import TestClient

    from app.main import app

    import app.api.chat as _chat_api
    _chat_api._guest_events.clear()

    with TestClient(app) as client:
        for i in range(15):
            r = _post(client, f"主人消息{i}")
            assert r.status_code == 200
            assert "聊得太快" not in r.json()["reply"]
    assert len(captured["messages"]) == 15
