"""聊天请求上下文层测试：身份解析、QQ 伪装主人、request_id 单飞缓存、访客限流。"""
import asyncio

import pytest

from app.chat.context import (
    GUEST_DAY_LIMIT,
    GUEST_WINDOW_LIMIT,
    ChatRequest,
    authenticated_uid,
    deduplicate_request,
    guest_rate_limited,
)
from app.chat.context import ChatResponse, _guest_events, _request_cache, _request_inflight
from app.config import settings
from app.core import memory as memory_module


def make_request(auth_role=None):
    state = type("State", (), {})()
    if auth_role is not None:
        state.auth = type("Auth", (), {"role": auth_role})()
    return type("Request", (), {"state": state})()


# ── 身份解析 ───────────────────────────────────────────────

def test_owner_internal_token_ignores_body_user_id():
    req = ChatRequest(message="hi", user_id="10086")
    for role in ("owner", "internal"):
        uid, is_owner = authenticated_uid(req, make_request(role), memory_module)
        assert uid == memory_module.owner_user_id()
        assert is_owner is True


def test_qq_token_resolves_guest(monkeypatch):
    monkeypatch.setattr(settings, "qq_admin_id", "123456")
    req = ChatRequest(message="hi", user_id="10086")
    uid, is_owner = authenticated_uid(req, make_request("qq"), memory_module)
    assert uid == "10086" and is_owner is False


def test_qq_token_cannot_impersonate_owner(monkeypatch):
    monkeypatch.setattr(settings, "qq_admin_id", "123456")
    for payload in ("owner", "123456"):
        req = ChatRequest(message="hi", user_id=payload)
        with pytest.raises(Exception) as exc:
            authenticated_uid(req, make_request("qq"), memory_module)
        assert getattr(exc.value, "status_code", None) == 403


def test_qq_token_invalid_user_id_rejected():
    req = ChatRequest(message="hi", user_id="abc123!")
    with pytest.raises(Exception) as exc:
        authenticated_uid(req, make_request("qq"), memory_module)
    assert getattr(exc.value, "status_code", None) == 400


def test_unknown_role_rejected():
    req = ChatRequest(message="hi")
    with pytest.raises(Exception) as exc:
        authenticated_uid(req, make_request("collector"), memory_module)
    assert getattr(exc.value, "status_code", None) == 403


def test_no_auth_falls_back_to_body_user_id():
    owner_req = ChatRequest(message="hi")
    uid, is_owner = authenticated_uid(owner_req, make_request(None), memory_module)
    assert uid == memory_module.owner_user_id() and is_owner is True

    guest_req = ChatRequest(message="hi", user_id="10086")
    uid, is_owner = authenticated_uid(guest_req, make_request(None), memory_module)
    assert uid == "10086" and is_owner is False


# ── request_id 单飞与成功缓存 ───────────────────────────────

@pytest.fixture(autouse=True)
def _clean_request_state():
    _request_cache.clear()
    _request_inflight.clear()
    yield
    _request_cache.clear()
    _request_inflight.clear()


def test_deduplicate_request_failure_not_cached():
    calls = []

    async def flaky_handler(req, request):
        calls.append(req.message)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return ChatResponse(reply="ok", memories_used=0)

    req = ChatRequest(message="m", request_id="rid-fail")
    request = make_request(None)

    async def run():
        with pytest.raises(RuntimeError):
            await deduplicate_request(req, request, memory_module, flaky_handler)
        return await deduplicate_request(req, request, memory_module, flaky_handler)

    resp = asyncio.run(run())
    assert resp.reply == "ok"
    assert calls == ["m", "m"], "失败后重试必须重新执行而不是复用失败"


def test_deduplicate_request_success_cached_per_user():
    calls = []

    async def handler(req, request):
        calls.append((req.user_id, req.message))
        return ChatResponse(reply=f"reply-{req.user_id}", memories_used=0)

    async def run():
        first = await deduplicate_request(
            ChatRequest(message="m", user_id="10086", request_id="rid-1"),
            make_request(None),
            memory_module,
            handler,
        )
        cached = await deduplicate_request(
            ChatRequest(message="m", user_id="10086", request_id="rid-1"),
            make_request(None),
            memory_module,
            handler,
        )
        other = await deduplicate_request(
            ChatRequest(message="m", user_id="20002", request_id="rid-1"),
            make_request(None),
            memory_module,
            handler,
        )
        return first, cached, other

    first, cached, other = asyncio.run(run())
    assert first.reply == cached.reply == "reply-10086"
    assert other.reply == "reply-20002"
    assert calls == [("10086", "m"), ("20002", "m")], "缓存按 (uid, request_id) 隔离"


# ── 访客限流 ───────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_guest_events():
    _guest_events.clear()
    yield
    _guest_events.clear()


def test_guest_rate_limited_window():
    for _ in range(GUEST_WINDOW_LIMIT):
        assert guest_rate_limited("10086") is False
    assert guest_rate_limited("10086") is True


def test_guest_rate_limited_day_cap():
    import time

    _guest_events["10086"] = __import__("collections").deque(
        [time.time()] * GUEST_DAY_LIMIT
    )
    assert guest_rate_limited("10086") is True


def test_guest_rate_limited_isolated_per_uid():
    assert guest_rate_limited("10086") is False
    assert guest_rate_limited("20002") is False
