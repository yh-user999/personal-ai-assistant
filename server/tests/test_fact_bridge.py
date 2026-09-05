"""设定结论补漏回归：记录指令/短肯定确认 → 从 AI 回复提取事实。"""
import asyncio

import pytest

from app.config import settings
from app.models.database import connect, init_db, reset_connections
from app.services import fact_extract


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    reset_connections()
    init_db()
    yield
    reset_connections()


# ── 纯函数判定 ────────────────────────────────────────────

def test_record_command_detection():
    assert fact_extract.is_record_command("先将这些记录下来")
    assert fact_extract.is_record_command("记录一下")
    assert fact_extract.is_record_command("把这个设定记下来")
    assert not fact_extract.is_record_command("今天记录了很多工作内容还开了两个会")  # 超长非指令
    assert not fact_extract.is_record_command("记录：下午调试")  # worklog 命令（handler 先拦）


def test_short_confirm_detection():
    assert fact_extract.is_short_confirm("对")
    assert fact_extract.is_short_confirm("好")
    assert fact_extract.is_short_confirm("就这样")
    assert not fact_extract.is_short_confirm("这个设定挺好，不过再改一下老人那句")


def test_last_ai_looks_like_setting():
    assert fact_extract.last_ai_looks_like_setting("那就把反击的第一步定下来：先隐忍照顾老人")
    assert not fact_extract.last_ai_looks_like_setting("好的，已记录你的工作日志")


def test_signal_words_include_record():
    assert "记录" in fact_extract.FACT_SIGNALS  # 旧触发表漏了"记录"（实测教训）


# ── 提取链路 ──────────────────────────────────────────────

def test_extract_from_last_ai(db_env, monkeypatch):
    captured = {}

    async def fake_chat(messages, **kwargs):
        captured["messages"] = messages
        return '[{"subject": "李羽", "predicate": "反击第一步", "object": "先隐忍照顾老人"}]'

    monkeypatch.setattr(fact_extract.llm, "chat", fake_chat)
    n = asyncio.run(fact_extract.extract_from_last_ai(
        "那就把反击的第一步定下来：先隐忍照顾老人", user_id=None
    ))
    assert n == 1
    # 提取源是 AI 回复（prompt 里包含该文本；messages[0] 是 system 角色）
    assert "先隐忍照顾老人" in captured["messages"][1]["content"]
    conn = connect()
    row = conn.execute(
        "SELECT subject, predicate, object FROM facts WHERE predicate='反击第一步'"
    ).fetchone()
    conn.close()
    assert row is not None and "隐忍" in row["object"]


def test_chat_record_command_triggers_extraction(db_env, monkeypatch):
    """「先将这些记录下来」→ 后台从上一条 AI 回复提取（不再漏）。"""
    import app.api.chat as _chat_api
    import app.core.embedding as _embedding

    conn = connect()
    conn.execute(
        "INSERT INTO memories (user_id, sender, content, ts) "
        "VALUES ('owner', 'assistant', '老人后续走向定下来：没挺过一周，临终遗言让他报仇。', "
        "'2026-09-02T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    extracted = {"n": 0}

    async def fake_extract(last_ai, user_id=None):
        extracted["n"] += 1
        return fact_extract.upsert_facts(
            [{"subject": "老人", "predicate": "后续走向", "object": "没挺过一周，临终遗言让他报仇"}],
            user_id=user_id,
        )

    async def fake_chat(messages, **kwargs):
        return "好的，已记录。"

    monkeypatch.setattr(_chat_api.llm, "chat", fake_chat)

    async def fake_embed(texts):
        return [[0.0] * settings.embedding_dimension for _ in texts]

    monkeypatch.setattr(_embedding, "embed", fake_embed)
    monkeypatch.setattr("app.services.fact_extract.extract_from_last_ai", fake_extract)

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/chat", json={"message": "先将这些记录下来"})
        assert r.status_code == 200

    # 后台任务异步执行，轮询等它落库
    import time

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        conn = connect()
        row = conn.execute(
            "SELECT 1 FROM facts WHERE subject='老人' AND predicate='后续走向'"
        ).fetchone()
        conn.close()
        if row:
            break
        time.sleep(0.05)
    assert extracted["n"] == 1
    conn = connect()
    row = conn.execute(
        "SELECT 1 FROM facts WHERE subject='老人' AND predicate='后续走向'"
    ).fetchone()
    conn.close()
    assert row is not None


def test_facts_injection_two_tier_window(db_env):
    """新写入的事实必须进注入窗口（旧实现只取最老 40 条，新事实永远不可见）。"""
    from app.core import memory

    conn = connect()
    # 造 35 条老事实（挤满锚点层）
    for i in range(35):
        conn.execute(
            "INSERT INTO facts (user_id, subject, predicate, object, updated_at) "
            "VALUES ('owner', ?, '老事实', ?, '2026-08-01T00:00:00+00:00')",
            (f"锚点{i}", f"内容{i}"),
        )
    conn.commit()
    conn.close()
    # 新写入一条（id 靠后）
    from app.services.fact_extract import upsert_facts

    upsert_facts([{"subject": "老人", "predicate": "后续走向", "object": "没挺过一周"}])
    text = memory.get_facts_injection(user_id=None)
    assert "老人 后续走向 没挺过一周" in text, "新事实未进入两层注入窗口"
    # 锚点层的老事实也仍在
    assert "锚点0 老事实" in text


def test_facts_injection_anchor_still_first(db_env):
    """锚点层保持 id ASC 稳定呈现（课程进度不因新事实被挤丢的旧教训）。"""
    from app.core import memory

    conn = connect()
    conn.execute(
        "INSERT INTO facts (user_id, subject, predicate, object, updated_at) "
        "VALUES ('owner', '六课带教计划', '状态', '第0-5课全部完成', '2026-08-20T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    text = memory.get_facts_injection(user_id=None)
    assert text.startswith("- 六课带教计划 状态 第0-5课全部完成")
