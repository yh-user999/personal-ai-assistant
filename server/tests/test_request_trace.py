"""检索可观测性 P0 回归：决策轨迹落库、意图级约束注入、开关与访客隔离。"""
import time

import pytest

from app.config import settings
from app.core import knowledge
from app.models.database import connect, init_db, reset_connections
from app.services import request_trace


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    reset_connections()
    init_db()
    yield
    reset_connections()


# ── 轨迹落库（纯函数）──────────────────────────────────────

def test_record_and_cleanup(db_env):
    assert request_trace.record(
        "10001", "命丛有哪些",
        {"domains": ["novel"], "docs": ["小说-寂静杀戮"]},
        "entity", True, ["命丛"], {"knowledge": 500, "entity": 200}, 42,
    )
    conn = connect()
    row = conn.execute(
        "SELECT * FROM request_traces WHERE query='命丛有哪些'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["retrieval_path"] == "entity"
    assert row["vector_degraded"] == 1
    assert row["routing"] == '{"domains": ["novel"], "docs": ["小说-寂静杀戮"]}'

    # 过期行清理：造一条 40 天前的旧行
    conn = connect()
    conn.execute(
        "INSERT INTO request_traces (user_id, query, ts) VALUES ('10001','旧问题', '2020-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    n = request_trace.cleanup_stale(days=30)
    assert n == 1
    conn = connect()
    left = [r["query"] for r in conn.execute("SELECT query FROM request_traces").fetchall()]
    conn.close()
    assert "旧问题" not in left and "命丛有哪些" in left


def test_record_failure_isolated(monkeypatch):
    """写库失败只记日志返回 False，不抛异常（后台任务的隔离性）。"""
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("app.services.request_trace.connect", boom)
    assert request_trace.record("u", "q", {}, "", False, [], {}, 0) is False


# ── chat 集成：主人落轨迹、访客不落、开关生效 ───────────────

@pytest.fixture
def chat_env(db_env, monkeypatch):
    import app.api.chat as _chat_api
    import app.core.embedding as _embedding

    async def fake_chat(messages, **kwargs):
        return "好的。"

    monkeypatch.setattr(_chat_api.llm, "chat", fake_chat)

    async def fake_embed(texts):
        return [[0.0] * settings.embedding_dimension for _ in texts]

    monkeypatch.setattr(_embedding, "embed", fake_embed)
    yield


def _post(client, message, user_id=None):
    payload = {"message": message}
    if user_id is not None:
        payload["user_id"] = user_id
    return client.post("/api/chat", json=payload)


def _wait_trace(query: str, timeout: float = 3.0) -> dict | None:
    """fire-and-forget 任务是异步的，轮询等它落库。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT * FROM request_traces WHERE query=? ORDER BY id DESC LIMIT 1",
                (query,),
            ).fetchone()
        finally:
            conn.close()
        if row is not None:
            return row
        time.sleep(0.05)
    return None


def test_owner_chat_writes_trace(chat_env):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = _post(client, "今天天气怎么样")
        assert r.status_code == 200
    row = _wait_trace("今天天气怎么样")
    assert row is not None
    assert row["user_id"] == "owner"
    assert row["retrieval_path"] in ("hybrid", "skip")


def test_guest_chat_no_trace(chat_env):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = _post(client, "访客的轨迹测试问题", user_id="10002")
        assert r.status_code == 200
    assert _wait_trace("访客的轨迹测试问题", timeout=1.0) is None


def test_trace_switch_disabled(chat_env, monkeypatch):
    monkeypatch.setattr(settings, "request_trace_enabled", False)
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = _post(client, "开关关闭的轨迹问题")
        assert r.status_code == 200
    assert _wait_trace("开关关闭的轨迹问题", timeout=1.0) is None


# ── 意图级约束注入 ─────────────────────────────────────────

def _seed_novel_chunk(conn, idx, content):
    cur = conn.execute(
        "INSERT INTO knowledge_chunks (doc_name, chunk_index, content, created_at) "
        "VALUES ('小说-寂静杀戮', ?, ?, '2026-09-01T00:00:00+00:00')",
        (idx, content),
    )
    conn.execute(
        "UPDATE knowledge_chunks SET domain='novel' WHERE id=?", (cur.lastrowid,)
    )
    conn.execute(
        "INSERT INTO knowledge_fts (chunk_id, grams) VALUES (?, ?)",
        (cur.lastrowid, knowledge._grams_text(content)),
    )
    conn.commit()


def test_intent_rules_injected_for_enum(db_env, monkeypatch):
    """枚举问题（已路由、无实体上下文）→ 注入 enum 约束；闲聊不注入。"""
    import app.api.chat as _chat_api
    import app.core.embedding as _embedding

    conn = connect()
    _seed_novel_chunk(conn, 1, "蜃宗是南圣门大阵里的势力，势力遍布各地。")
    conn.close()

    box = {"systems": []}

    async def fake_chat(messages, **kwargs):
        box["systems"].append(messages[0]["content"])
        return "好的。"

    monkeypatch.setattr(_chat_api.llm, "chat", fake_chat)

    async def fake_embed(texts):
        return [[0.0] * settings.embedding_dimension for _ in texts]

    monkeypatch.setattr(_embedding, "embed", fake_embed)

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        # 势力是静态类名 → 路由 novel；无势力实体 → 无实体上下文 → enum 标签
        _post(client, "有哪些势力")
        _post(client, "今天天气怎么样")

    assert "可能不全" in box["systems"][0]   # enum 约束
    assert "可能不全" not in box["systems"][1]  # 闲聊零约束
