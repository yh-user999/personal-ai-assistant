"""自建 OneBot 网关的确定性门禁、契约和发送回归。"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import sign_qq_identity as server_sign_qq_identity
from qq.onebot_gateway.app import Gateway, create_app
from qq.onebot_gateway.chat import ChatClient, ChatResult
from qq.onebot_gateway.config import GatewaySettings, parse_group_allowlist
from qq.onebot_gateway.event import OneBotMessage
from qq.onebot_gateway.gate import (
    DeliveryCache,
    FollowupStore,
    GroupReplyLimiter,
    is_group_directed,
)
from qq.onebot_gateway.onebot import OneBotClient


def _event(
    message,
    *,
    message_id: str = "1",
    message_type: str = "group",
    user_id: str = "42",
    group_id: str = "99",
    self_id: str = "100",
):
    payload = {
        "post_type": "message",
        "message_type": message_type,
        "user_id": user_id,
        "message_id": message_id,
        "self_id": self_id,
        "message": message,
    }
    if message_type == "group":
        payload["group_id"] = group_id
    return payload


class FakeOneBot:
    def __init__(self, senders=None):
        self.senders = dict(senders or {})
        self.group_messages = []
        self.private_messages = []

    async def get_message_sender(self, message_id):
        value = self.senders.get(str(message_id))
        if isinstance(value, Exception):
            raise value
        return value

    async def send_group(self, group_id, message):
        self.group_messages.append((group_id, message))

    async def send_private(self, user_id, message):
        self.private_messages.append((user_id, message))

    async def aclose(self):
        return None


class FakeChat:
    def __init__(self, interaction=None):
        self.calls = []
        self.interaction = interaction or {}

    async def chat(self, message, *, group_directed=False):
        self.calls.append((message, group_directed))
        return ChatResult("回复", self.interaction)

    async def aclose(self):
        return None


def _settings(**overrides):
    values = dict(
        server_api_base="http://server.test",
        onebot_api_url="http://napcat.test",
        onebot_token="onebot-token",
        inbound_token="gateway-token",
        qq_api_token="qq-api-token",
        identity_secret="identity-secret",
        owner_api_token="owner-api-token",
        owner_id="7",
        group_allowed_ids=frozenset({"99"}),
        group_max_replies_per_hour=30,
    )
    values.update(overrides)
    return GatewaySettings(**values)


def test_parse_group_allowlist_is_fail_closed_by_default():
    assert parse_group_allowlist("") == (False, frozenset())
    assert parse_group_allowlist("99, 100;101") == (False, frozenset({"99", "100", "101"}))
    assert parse_group_allowlist("*") == (True, frozenset())


def test_event_parser_rejects_untrusted_message_ids_and_malformed_segments():
    assert OneBotMessage.from_payload(_event("你好", message_id="1\\n2")) is None
    message = OneBotMessage.from_payload(
        _event(
            [
                {"type": "text", "data": "malformed"},
                {"type": "text", "data": {"text": "安全正文"}},
            ]
        )
    )
    assert message is not None
    assert message.text == "安全正文"

    cq_at = OneBotMessage.from_payload(_event("[CQ:at,qq=100] 你好"))
    cq_reply = OneBotMessage.from_payload(_event("[CQ:reply,id=8] 继续"))
    assert cq_at is not None and cq_at.at_targets == ("100",)
    assert cq_reply is not None and cq_reply.reply_ids == ("8",)


@pytest.mark.asyncio
async def test_real_at_prefix_and_reply_are_directed_but_name_is_not():
    reply_sender = {"8": "100"}

    async def resolve(message_id):
        return reply_sender.get(message_id)

    at = OneBotMessage.from_payload(_event([{"type": "at", "data": {"qq": "100"}}]))
    other = OneBotMessage.from_payload(_event([{"type": "at", "data": {"qq": "101"}}]))
    prefix = OneBotMessage.from_payload(_event("小月 你好"))
    reply = OneBotMessage.from_payload(_event([{"type": "reply", "data": {"id": "8"}}]))
    name = OneBotMessage.from_payload(_event("小月最近怎么样"))

    assert await is_group_directed(at, self_id="100", require_mention=True, prefix="", reply_sender=resolve)
    assert not await is_group_directed(other, self_id="100", require_mention=True, prefix="", reply_sender=resolve)
    assert await is_group_directed(prefix, self_id="100", require_mention=True, prefix="小月", reply_sender=resolve)
    assert await is_group_directed(reply, self_id="100", require_mention=True, prefix="", reply_sender=resolve)
    assert not await is_group_directed(name, self_id="100", require_mention=True, prefix="", reply_sender=resolve)


@pytest.mark.asyncio
async def test_reply_lookup_failure_is_fail_closed():
    async def resolve(_message_id):
        raise RuntimeError("lookup failed")

    message = OneBotMessage.from_payload(_event([{"type": "reply", "data": {"id": "8"}}]))
    assert message is not None
    assert not await is_group_directed(
        message,
        self_id="100",
        require_mention=True,
        prefix="",
        reply_sender=resolve,
    )


@pytest.mark.asyncio
async def test_gateway_sets_group_directed_and_deduplicates_delivery():
    onebot = FakeOneBot()
    chat = FakeChat({"followup": {"expires_in": 90, "max_messages": 1}})
    gateway = Gateway(_settings(), onebot=onebot, chat=chat)

    direct = _event(
        [
            {"type": "at", "data": {"qq": "100"}},
            {"type": "text", "data": {"text": " 你好"}},
        ],
        message_id="1",
    )
    first = await gateway.handle(direct)
    duplicate = await gateway.handle(direct)
    chat.interaction = {}
    followup = await gateway.handle(_event("补充说明", message_id="2"))
    after_followup = await gateway.handle(_event("再次补充", message_id="3"))

    assert first == {"status": "ok", "handled": True, "directed": True, "sent": True}
    assert duplicate == {"status": "ok", "duplicate": True, "sent": False}
    assert followup["directed"] is True
    assert after_followup == {"status": "ignored", "reason": "not_directed"}
    assert len(onebot.group_messages) == 2
    assert [directed for _message, directed in chat.calls] == [True, True]


@pytest.mark.asyncio
async def test_gateway_rejects_other_groups_and_non_directed_messages():
    onebot = FakeOneBot()
    chat = FakeChat()
    gateway = Gateway(_settings(), onebot=onebot, chat=chat)

    other_group = await gateway.handle(_event("你好", group_id="101", message_id="1"))
    ordinary = await gateway.handle(_event("你好", message_id="2"))

    assert other_group == {"status": "ignored", "reason": "group_not_allowed"}
    assert ordinary == {"status": "ignored", "reason": "not_directed"}
    assert chat.calls == []
    assert onebot.group_messages == []


@pytest.mark.asyncio
async def test_gateway_rejects_missing_or_mismatched_configured_self_id():
    onebot = FakeOneBot()
    chat = FakeChat()
    gateway = Gateway(_settings(configured_self_id="100"), onebot=onebot, chat=chat)
    direct = [
        {"type": "at", "data": {"qq": "100"}},
        {"type": "text", "data": {"text": " 你好"}},
    ]

    missing = await gateway.handle(_event(direct, message_id="1", self_id=""))
    mismatched = await gateway.handle(_event(direct, message_id="2", self_id="101"))

    assert missing == {"status": "ignored", "reason": "not_directed"}
    assert mismatched == {"status": "ignored", "reason": "not_directed"}
    assert chat.calls == []


@pytest.mark.asyncio
async def test_gateway_keeps_group_hourly_limit_for_directed_messages():
    onebot = FakeOneBot()
    chat = FakeChat()
    gateway = Gateway(_settings(group_max_replies_per_hour=1), onebot=onebot, chat=chat)

    first = await gateway.handle(
        _event(
            [
                {"type": "at", "data": {"qq": "100"}},
                {"type": "text", "data": {"text": " 你好"}},
            ],
            message_id="1",
        )
    )
    second = await gateway.handle(
        _event(
            [
                {"type": "at", "data": {"qq": "100"}},
                {"type": "text", "data": {"text": " 再问"}},
            ],
            message_id="2",
        )
    )

    assert first["sent"] is True
    assert second == {"status": "ignored", "reason": "group_rate_limited"}
    assert len(onebot.group_messages) == 1


@pytest.mark.asyncio
async def test_chat_client_preserves_existing_hmac_contract_and_body():
    captured = {}

    async def handler(request: httpx.Request):
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"reply": "ok", "interaction": {}})

    settings = _settings()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        chat = ChatClient(settings, client=client)
        message = OneBotMessage.from_payload(
            _event("你好", message_type="group", message_id="123")
        )
        result = await chat.chat(message, group_directed=True)
    finally:
        await client.aclose()

    assert result.reply == "ok"
    assert captured["body"] == {
        "message": "你好",
        "request_id": "onebot-123",
        "user_id": "42",
        "group_id": "99",
        "group_directed": True,
    }
    assert captured["headers"]["x-qq-user-id"] == "42"
    assert captured["headers"]["x-qq-request-id"] == "onebot-123"
    assert captured["headers"]["x-qq-signature"] == server_sign_qq_identity(
        "identity-secret",
        "42",
        captured["headers"]["x-qq-timestamp"],
        "onebot-123",
    )


@pytest.mark.asyncio
async def test_chat_client_uses_owner_token_without_guest_hmac():
    captured = {}

    async def handler(request: httpx.Request):
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"reply": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        chat = ChatClient(_settings(), client=client)
        owner = OneBotMessage.from_payload(
            _event("你好", message_type="private", user_id="7", group_id="", message_id="9")
        )
        await chat.chat(owner)
    finally:
        await client.aclose()

    assert captured["headers"]["authorization"] == "Bearer owner-api-token"
    assert "x-qq-signature" not in captured["headers"]


@pytest.mark.asyncio
async def test_onebot_client_calls_get_msg_and_send_group():
    calls = []

    async def handler(request: httpx.Request):
        payload = json.loads(request.content)
        calls.append(payload)
        if payload["action"] == "get_msg":
            return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {"sender": {"user_id": 100}}})
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        onebot = OneBotClient("http://napcat.test", "onebot-token", client=client)
        assert await onebot.get_message_sender("8") == "100"
        await onebot.send_group("99", "回复")
    finally:
        await client.aclose()

    assert calls == [
        {"action": "get_msg", "params": {"message_id": 8}},
        {"action": "send_group_msg", "params": {"group_id": 99, "message": "回复"}},
    ]


def test_gateway_http_endpoint_checks_inbound_token():
    gateway = Gateway(_settings(), onebot=FakeOneBot(), chat=FakeChat())
    application = create_app(gateway=gateway)
    with TestClient(application) as client:
        rejected = client.post("/onebot", json=_event("你好"), headers={"Authorization": "Bearer wrong"})
        accepted = client.post(
            "/onebot",
            json={"post_type": "notice", "notice_type": "group_upload"},
            headers={"Authorization": "Bearer gateway-token"},
        )
    assert rejected.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json() == {"status": "ignored", "reason": "unsupported_event"}


def test_followup_and_delivery_stores_are_bounded_and_expirable():
    followups = FollowupStore(default_expires_in=5, default_max_messages=1)
    followups.remember("99", "42", {"followup": {"expires_in": 90, "max_messages": 4}}, now=0)
    assert followups.consume("99", "42", now=4) is True
    assert followups.consume("99", "42", now=4) is False

    deliveries = DeliveryCache(ttl_seconds=5, maximum=1)
    deliveries.mark("one", now=0)
    deliveries.mark("two", now=1)
    assert deliveries.seen("one", now=1) is False
    assert deliveries.seen("two", now=6) is False

    limiter = GroupReplyLimiter()
    reservation = limiter.reserve("99", cooldown_seconds=10, max_replies_per_hour=1, now=0)
    assert reservation is not None
    assert limiter.reserve("99", cooldown_seconds=10, max_replies_per_hour=1, now=1) is None
    limiter.release(reservation)
    assert limiter.reserve("99", cooldown_seconds=10, max_replies_per_hour=1, now=1) is not None

    bounded_followups = FollowupStore(maximum_items=1)
    bounded_followups.remember("99", "42", {"followup": {}}, now=0)
    bounded_followups.remember("100", "42", {"followup": {}}, now=0)
    assert bounded_followups.consume("99", "42", now=0) is True
    assert bounded_followups.consume("100", "42", now=0) is False

    bounded_limiter = GroupReplyLimiter(maximum_groups=1)
    bounded_reservation = bounded_limiter.reserve("99", cooldown_seconds=0, max_replies_per_hour=1, now=0)
    assert bounded_reservation is not None
    bounded_limiter.commit(bounded_reservation)
    assert bounded_limiter.reserve("100", cooldown_seconds=0, max_replies_per_hour=1, now=0) is None
    assert bounded_limiter.reserve("100", cooldown_seconds=0, max_replies_per_hour=1, now=3601) is not None
