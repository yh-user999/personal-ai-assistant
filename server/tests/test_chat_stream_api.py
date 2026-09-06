"""SSE 流式端点集成测试：事件序列、最终 done 全文、图片请求拒绝。

走完整 /api/chat/stream 组装路径（mock llm.chat_stream），锁定前端依赖的
事件契约：meta → delta* → done，done.reply 为服务端清洗后的最终全文。
"""
import json

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
def stream_llm(monkeypatch):
    import app.api.chat as _chat_api

    async def fake_stream(messages, **kwargs):
        yield "你"
        yield "好"

    monkeypatch.setattr(_chat_api.llm, "chat_stream", fake_stream)

    # embedding 是单例模块，memory/knowledge 共享同一对象——一次打点全覆盖
    import app.core.embedding as _embedding

    async def fake_embed(texts):
        return [[0.0] * settings.embedding_dimension for _ in texts]

    monkeypatch.setattr(_embedding, "embed", fake_embed)
    return fake_stream


def _parse_sse(lines):
    """把 SSE 行流解析成 [(event, data_dict)]。"""
    events = []
    event = None
    for line in lines:
        if not line:
            event = None
            continue
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and event:
            events.append((event, json.loads(line.split(":", 1)[1])))
    return events


def test_stream_endpoint_event_sequence(env, stream_llm):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        with client.stream(
            "POST", "/api/chat/stream", json={"message": "打个招呼"}
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            events = _parse_sse(resp.iter_lines())

    kinds = [event for event, _ in events]
    assert kinds[0] == "meta", "首个事件必须是 meta"
    assert kinds[-1] == "done", "末尾事件必须是 done"
    deltas = [data["text"] for event, data in events if event == "delta"]
    assert deltas == ["你", "好"]
    done = dict(events)["done"]
    assert done["reply"] == "你好"
    assert done["memories_used"] == 0

    # 与全量路径一致的持久化语义：双方消息入库
    conn = connect()
    try:
        senders = {
            row["sender"]
            for row in conn.execute(
                "SELECT sender FROM memories WHERE content IN ('打个招呼', '你好')"
            ).fetchall()
        }
    finally:
        conn.close()
    assert {"user", "assistant"} <= senders


def test_stream_endpoint_rejects_image(env, stream_llm):
    """图片提问不支持流式：边界直接 400，不进入主链路。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        resp = client.post(
            "/api/chat/stream",
            json={
                "message": "看图",
                "image": {
                    "media_type": "image/png",
                    "sha256": "a" * 64,
                    "size": 10,
                    "data_url": "data:image/png;base64,AAAA",
                },
            },
        )
    assert resp.status_code == 400
    assert "vision" in resp.json()["detail"]["message"]  # detail 已结构化为 {code, message}
