"""LLM 用量持久化和用户范围回归。"""
import asyncio
from types import SimpleNamespace

import pytest

from app.config import settings
from app.core import llm
from app.models.database import connect, init_db, reset_connections
from app.services.llm_usage import record, window_totals


@pytest.fixture
def usage_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "usage.db"))
    reset_connections()
    init_db()
    yield
    reset_connections()


def test_chat_and_json_persist_subject_request_and_fallback(usage_db, monkeypatch):
    class StatusError(Exception):
        status_code = 429

    calls = []

    def factory(**kwargs):
        key_index = int(kwargs["api_key"].rsplit("-", 1)[-1])

        async def create(**request_kwargs):
            calls.append((key_index, request_kwargs))
            if key_index == 0:
                raise StatusError()
            content = '{"ok": true}' if request_kwargs.get("response_format") else "ok"
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=12, completion_tokens=4),
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            )

        return SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        )

    monkeypatch.setattr(settings, "llm_api_keys", "test-key-0,test-key-1")
    monkeypatch.setattr(settings, "llm_max_retries", 1)
    monkeypatch.setattr(settings, "llm_key_cooldown_seconds", 0.0)
    monkeypatch.setattr(llm, "AsyncOpenAI", factory)
    llm.reset_client_pool()

    assert asyncio.run(
        llm.chat(
            [{"role": "user", "content": "hello"}],
            model="test-model",
            request_id="chat-request",
            user_id="123",
        )
    ) == "ok"
    assert asyncio.run(
        llm.chat_json(
            "system",
            "user",
            model="test-model",
            request_id="json-request",
            user_id=None,
        )
    ) == {"ok": True}

    assert [key_index for key_index, _ in calls] == [0, 1, 0, 1]
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT request_id, user_id, fallback_count, prompt_tokens, completion_tokens "
            "FROM llm_usage ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert [dict(row) for row in rows] == [
        {
            "request_id": "chat-request",
            "user_id": "123",
            "fallback_count": 1,
            "prompt_tokens": 12,
            "completion_tokens": 4,
        },
        {
            "request_id": "json-request",
            "user_id": "owner",
            "fallback_count": 1,
            "prompt_tokens": 12,
            "completion_tokens": 4,
        },
    ]


def test_usage_survives_connection_reset_and_stays_user_scoped(usage_db):
    record(
        request_id="owner-request",
        user_id=None,
        model="test-model",
        key_index=0,
        prompt_tokens=100,
        completion_tokens=20,
        cached_tokens=5,
        fallback_count=0,
    )
    record(
        request_id="guest-request",
        user_id="123",
        model="test-model",
        key_index=1,
        prompt_tokens=200,
        completion_tokens=40,
        cached_tokens=10,
        fallback_count=1,
    )

    assert window_totals(user_id="owner") == {
        "calls": 1,
        "prompt": 100,
        "completion": 20,
        "cached": 5,
        "fallback_count": 0,
    }
    assert window_totals(user_id="123") == {
        "calls": 1,
        "prompt": 200,
        "completion": 40,
        "cached": 10,
        "fallback_count": 1,
    }

    reset_connections()
    assert window_totals(user_id="123")["prompt"] == 200
