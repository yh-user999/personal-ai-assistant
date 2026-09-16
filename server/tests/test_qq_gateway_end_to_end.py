"""QQ 网关 → 真实 FastAPI 的端到端链路回归。

与 test_qq_onebot_gateway.py 的区别：那里用假 ChatClient 只验网关内部门禁，
这里让网关的 ChatClient 通过 ASGI 直连**真实** /api/chat，把身份 HMAC 校验、
群作用域、request_id 幂等、群聊闸门和回复审校全部串起来跑，只桩掉 LLM 与
embedding。目的是把需要真机扫码的人工验收面压到只剩"NapCat 是否把消息发出去"。
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import settings
from app.main import app as server_app
from app.models.database import init_db, reset_connections
from qq.onebot_gateway.app import Gateway
from qq.onebot_gateway.chat import ChatClient
from qq.onebot_gateway.config import GatewaySettings
from tests.llm_doubles import internal_call_response

GUEST = "10086"
GROUP = "916000001"
OTHER_GROUP = "916000002"
SELF_ID = "916100100"
REPLY_TEXT = "我是小月。"


@pytest.fixture
def server_env(tmp_path, monkeypatch):
    """真实服务端：隔离库 + QQ 访客鉴权配置 + 桩 LLM/embedding。"""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "e2e.db"))
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "owner_api_token", "owner-token")
    monkeypatch.setattr(settings, "internal_api_token", "")
    monkeypatch.setattr(settings, "collector_api_token", "")
    monkeypatch.setattr(settings, "executor_api_token", "")
    monkeypatch.setattr(settings, "qq_api_token", "qq-token")
    monkeypatch.setattr(settings, "qq_identity_secret", "identity-secret")
    monkeypatch.setattr(settings, "qq_identity_max_age_seconds", 300)
    monkeypatch.setattr(settings, "qq_admin_id", "")

    async def fake_chat(messages, **kwargs):
        stub = internal_call_response(kwargs)
        return REPLY_TEXT if stub is None else stub

    import app.api.chat as _chat_api
    import app.core.embedding as _embedding

    monkeypatch.setattr(_chat_api.llm, "chat", fake_chat)

    async def fake_embed(texts):
        return [[0.0] * settings.embedding_dimension for _ in texts]

    monkeypatch.setattr(_embedding, "embed", fake_embed)

    reset_connections()
    init_db()
    yield
    reset_connections()


class RecordingOneBot:
    """记录 NapCat 出口调用，不发真实请求。"""

    def __init__(self, senders: dict[str, str] | None = None) -> None:
        self.senders = dict(senders or {})
        self.group_messages: list[tuple[str, str]] = []
        self.private_messages: list[tuple[str, str]] = []

    async def get_message_sender(self, message_id):
        return self.senders.get(str(message_id))

    async def send_group(self, group_id, message):
        self.group_messages.append((group_id, message))

    async def send_private(self, user_id, message):
        self.private_messages.append((user_id, message))

    async def aclose(self):
        return None


def _gateway_settings(**overrides) -> GatewaySettings:
    values = dict(
        server_api_base="http://server.local",
        onebot_api_url="http://napcat.local",
        onebot_token="onebot-token",
        inbound_token="gateway-token",
        qq_api_token="qq-token",
        identity_secret="identity-secret",
        owner_api_token="owner-token",
        owner_id="",
        configured_self_id=SELF_ID,
        group_allowed_ids=frozenset({GROUP}),
        group_max_replies_per_hour=30,
    )
    values.update(overrides)
    return GatewaySettings(**values)


def _build(onebot: RecordingOneBot, **overrides) -> tuple[Gateway, httpx.AsyncClient]:
    """网关的 ChatClient 走 ASGI 直连真实 FastAPI。"""
    config = _gateway_settings(**overrides)
    transport = httpx.ASGITransport(app=server_app)
    client = httpx.AsyncClient(transport=transport, base_url=config.server_api_base)
    gateway = Gateway(config, onebot=onebot, chat=ChatClient(config, client=client))
    return gateway, client


def _event(message, *, message_id: str, message_type: str = "group", user_id: str = GUEST,
           group_id: str = GROUP, self_id: str = SELF_ID) -> dict:
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


def _at(text: str = " 你是谁") -> list[dict]:
    return [
        {"type": "at", "data": {"qq": SELF_ID}},
        {"type": "text", "data": {"text": text}},
    ]


def _run(coro):
    return asyncio.run(coro)


def test_real_at_reaches_server_and_reply_is_sent(server_env):
    """真实 At → 服务端真链路 → NapCat 群发送出口。"""
    onebot = RecordingOneBot()

    async def scenario():
        gateway, client = _build(onebot)
        try:
            return await gateway.handle(_event(_at(), message_id="1001"))
        finally:
            await client.aclose()

    result = _run(scenario())

    assert result["status"] == "ok"
    assert result["directed"] is True
    assert result["sent"] is True
    assert len(onebot.group_messages) == 1
    group_id, text = onebot.group_messages[0]
    assert group_id == GROUP
    assert text.strip(), "服务端回复不得为空"


def test_reply_to_bot_is_directed_but_reply_to_others_is_not(server_env):
    """Reply 目标是本账号才算直达；引用他人消息不触发。"""
    onebot = RecordingOneBot(senders={"9001": SELF_ID, "9002": "916200200"})

    async def scenario():
        gateway, client = _build(onebot)
        try:
            to_bot = await gateway.handle(
                _event([{"type": "reply", "data": {"id": "9001"}},
                        {"type": "text", "data": {"text": "继续说"}}], message_id="1002")
            )
            to_other = await gateway.handle(
                _event([{"type": "reply", "data": {"id": "9002"}},
                        {"type": "text", "data": {"text": "你说得对"}}], message_id="1003")
            )
            return to_bot, to_other
        finally:
            await client.aclose()

    to_bot, to_other = _run(scenario())

    assert to_bot["directed"] is True and to_bot["sent"] is True
    assert to_other == {"status": "ignored", "reason": "not_directed"}
    assert len(onebot.group_messages) == 1


def test_prefix_triggers_and_plain_name_stays_silent(server_env):
    """配置前缀放行；只在正文提到名字不触发，且不调用服务端。"""
    onebot = RecordingOneBot()

    async def scenario():
        gateway, client = _build(onebot, group_trigger_prefix="小月")
        try:
            prefix = await gateway.handle(
                _event([{"type": "text", "data": {"text": "小月 帮我看下"}}], message_id="1004")
            )
            name_only = await gateway.handle(
                _event([{"type": "text", "data": {"text": "刚才谁提到小月了"}}], message_id="1005")
            )
            return prefix, name_only
        finally:
            await client.aclose()

    prefix, name_only = _run(scenario())

    assert prefix["directed"] is True and prefix["sent"] is True
    assert name_only == {"status": "ignored", "reason": "not_directed"}
    assert len(onebot.group_messages) == 1


def test_non_allowlisted_group_never_reaches_server(server_env):
    """白名单外的群直接静默，连服务端都不调用。"""
    onebot = RecordingOneBot()

    async def scenario():
        gateway, client = _build(onebot)
        try:
            return await gateway.handle(
                _event(_at(), message_id="1006", group_id=OTHER_GROUP)
            )
        finally:
            await client.aclose()

    assert _run(scenario()) == {"status": "ignored", "reason": "group_not_allowed"}
    assert onebot.group_messages == []


def test_duplicate_event_is_not_sent_twice(server_env):
    """NapCat 重投同一 message_id 时不重复发送。"""
    onebot = RecordingOneBot()

    async def scenario():
        gateway, client = _build(onebot)
        try:
            event = _event(_at(), message_id="1007")
            first = await gateway.handle(event)
            second = await gateway.handle(event)
            return first, second
        finally:
            await client.aclose()

    first, second = _run(scenario())

    assert first["sent"] is True
    assert second == {"status": "ok", "duplicate": True, "sent": False}
    assert len(onebot.group_messages) == 1, "重复事件不得造成二次发送"


def test_group_hourly_limit_blocks_second_directed_message(server_env):
    """每群小时上限对直达消息同样生效（滥用闸门）。"""
    onebot = RecordingOneBot()

    async def scenario():
        gateway, client = _build(onebot, group_max_replies_per_hour=1)
        try:
            first = await gateway.handle(_event(_at(" 第一次"), message_id="1008"))
            second = await gateway.handle(_event(_at(" 第二次"), message_id="1009"))
            return first, second
        finally:
            await client.aclose()

    first, second = _run(scenario())

    assert first["sent"] is True
    assert second == {"status": "ignored", "reason": "group_rate_limited"}
    assert len(onebot.group_messages) == 1


def test_group_turn_does_not_leak_private_memory(server_env):
    """群作用域落库隔离：群轮次不得把消息写进私聊作用域。"""
    onebot = RecordingOneBot()

    async def scenario():
        gateway, client = _build(onebot)
        try:
            return await gateway.handle(_event(_at(" 群里问一句"), message_id="1010"))
        finally:
            await client.aclose()

    assert _run(scenario())["sent"] is True

    from app.models.database import connect

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT group_id, COUNT(*) AS c FROM memories WHERE user_id=? GROUP BY group_id",
            (GUEST,),
        ).fetchall()
    finally:
        conn.close()
    by_scope = {row["group_id"]: row["c"] for row in rows}
    assert by_scope.get(GROUP, 0) > 0, "群消息应落在群作用域"
    assert by_scope.get("", 0) == 0, "群轮次不得写入私聊作用域"


def test_auth_failure_is_silent_but_upstream_outage_notifies(server_env):
    """区分两类上游失败：鉴权/配置错误静默，临时故障才回提示。

    这是端到端验证时发现的问题：HMAC 配错会让每条 @ 都换来一句"服务不可达"，
    等于把服务器配置故障广播给整个群，且重试不可能成功。
    """
    onebot = RecordingOneBot()

    async def scenario():
        # 网关密钥与服务端不一致 → 真实服务端返回 401
        bad_auth, bad_client = _build(onebot, identity_secret="WRONG-secret")
        try:
            auth_failed = await bad_auth.handle(_event(_at(), message_id="1013"))
        finally:
            await bad_client.aclose()

        # 服务端不可达（连接错误）→ 属于临时故障
        outage_config = _gateway_settings()
        broken = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(httpx.ConnectError("boom"))
            ),
            base_url=outage_config.server_api_base,
        )
        outage_gateway = Gateway(
            outage_config, onebot=onebot, chat=ChatClient(outage_config, client=broken)
        )
        try:
            outage = await outage_gateway.handle(_event(_at(), message_id="1014"))
        finally:
            await broken.aclose()
        return auth_failed, outage

    auth_failed, outage = _run(scenario())

    assert auth_failed["sent"] is False, "鉴权/配置错误不得向群里发提示"
    assert outage["sent"] is True, "临时故障仍应告知用户"
    assert len(onebot.group_messages) == 1
    assert "不可达" in onebot.group_messages[0][1]


def test_missing_self_id_is_fail_closed(server_env):
    """配置了本账号 ID 时，事件缺失/不匹配 self_id 一律拒绝。"""
    onebot = RecordingOneBot()

    async def scenario():
        gateway, client = _build(onebot)
        try:
            missing = await gateway.handle(_event(_at(), message_id="1011", self_id=""))
            mismatched = await gateway.handle(
                _event(_at(), message_id="1012", self_id="916999999")
            )
            return missing, mismatched
        finally:
            await client.aclose()

    missing, mismatched = _run(scenario())

    assert missing == {"status": "ignored", "reason": "not_directed"}
    assert mismatched == {"status": "ignored", "reason": "not_directed"}
    assert onebot.group_messages == []
