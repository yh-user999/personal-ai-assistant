"""QQ 图片链路的轻量测试；只用 AstrBot 模块桩，不依赖运行中的 AstrBot。"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import httpx
import pytest


def _install_astrbot_stubs():
    if "astrbot.api" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    components = types.ModuleType("astrbot.api.message_components")
    star = types.ModuleType("astrbot.api.star")
    result = types.ModuleType("astrbot.core.message.message_event_result")

    class _Filter:
        class EventMessageType:
            ALL = "all"

        @staticmethod
        def event_message_type(_kind):
            return lambda fn: fn

    class Plain:
        def __init__(self, text):
            self.text = text

    class Image:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class At:
        type = "At"

        def __init__(self, qq):
            self.qq = qq

    class File:
        pass

    class MessageChain(list):
        pass

    class Star:
        def __init__(self, _context=None):
            pass

    def register(*_args, **_kwargs):
        return lambda cls: cls

    api.logger = types.SimpleNamespace(debug=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None)
    api.AstrBotConfig = dict
    event.AstrMessageEvent = object
    event.filter = _Filter
    components.Plain = Plain
    components.Image = Image
    components.At = At
    components.File = File
    star.Context = object
    star.Star = Star
    star.register = register
    result.MessageChain = MessageChain
    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.message_components": components,
        "astrbot.api.star": star,
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.message": types.ModuleType("astrbot.core.message"),
        "astrbot.core.message.message_event_result": result,
    })


_install_astrbot_stubs()
_SPEC = importlib.util.spec_from_file_location("qq_xy_test_module", Path(__file__).with_name("main.py"))
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MOD
_SPEC.loader.exec_module(_MOD)


class _Event:
    def __init__(self, messages, text="", sender="123", group="", self_id="999"):
        self._messages = messages
        self._text = text
        self._sender = sender
        self._group = group
        self._self_id = self_id
        self.sent = []
        self.stopped = False
        self.llm_blocked = False

    def get_messages(self):
        return self._messages

    def get_message_str(self):
        return self._text

    def get_sender_id(self):
        return self._sender

    def get_group_id(self):
        return self._group

    def get_self_id(self):
        return self._self_id

    def stop_event(self):
        self.stopped = True

    def should_call_llm(self, value):
        self.llm_blocked = value

    async def send(self, chain):
        self.sent.append(chain)


class _StreamResponse:
    def __init__(self, body, content_type="image/png", content_length=None, error=None):
        self.headers = {"content-type": content_type}
        if content_length is not None:
            self.headers["content-length"] = str(content_length)
        self.body = body
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def aiter_bytes(self):
        for i in range(0, len(self.body), 3):
            yield self.body[i : i + 3]


class _StreamClient:
    def __init__(self, response):
        self.response = response

    def stream(self, *_args, **_kwargs):
        return self.response


class _PostResponse:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {"reply": "看到了"}
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.payload


class _PostClient:
    def __init__(self, response=None):
        self.response = response or _PostResponse()
        self.kwargs = None

    async def post(self, *args, **kwargs):
        self.kwargs = kwargs
        return self.response


def _plugin(client=None, max_bytes=10 * 1024 * 1024):
    plugin = _MOD.XiaoYuePlugin.__new__(_MOD.XiaoYuePlugin)
    plugin.cfg = {
        "api_base": "http://local",
        "api_token": "qq-token",
        "owner_api_token": "owner-token",
        "owner_qq": "123",
        "identity_secret": "secret",
        "assistant_mode": "personal",
        "group_allowed_ids": "456",
        "group_require_mention": True,
        "group_cooldown_seconds": 60,
        "group_max_replies_per_hour": 6,
    }
    plugin._vision_timeout_seconds = 90
    plugin._vision_max_image_bytes = max_bytes
    plugin._client = client or _PostClient()
    plugin._proxy_client = client or _PostClient()
    return plugin


def test_owner_and_visitor_use_separate_api_tokens_and_headers():
    assert _MOD.select_api_token("123", "123", "owner-token", "qq-token") == (
        "owner-token",
        True,
    )
    assert _MOD.select_api_token("456", "123", "owner-token", "qq-token") == (
        "qq-token",
        False,
    )
    # 主人 token 缺失时也不能退回 QQ 访客 token。
    assert _MOD.select_api_token("123", "123", "", "qq-token") == ("", True)

    plugin = _plugin()
    owner_headers = plugin._api_headers("123", "owner-request")
    assert owner_headers["Authorization"] == "Bearer owner-token"
    assert "X-QQ-User-ID" not in owner_headers

    visitor_headers = plugin._api_headers("456", "visitor-request")
    assert visitor_headers["Authorization"] == "Bearer qq-token"
    assert visitor_headers["X-QQ-User-ID"] == "456"
    assert visitor_headers["X-QQ-Request-ID"] == "visitor-request"


def test_text_request_routes_owner_and_visitor_tokens():
    client = _PostClient()
    plugin = _plugin(client)

    owner_event = _Event([], "主人消息", sender="123")
    asyncio.run(plugin.on_message(owner_event))
    assert client.kwargs["headers"]["Authorization"] == "Bearer owner-token"
    assert "X-QQ-User-ID" not in client.kwargs["headers"]

    visitor_event = _Event([], "访客消息", sender="456")
    asyncio.run(plugin.on_message(visitor_event))
    assert client.kwargs["headers"]["Authorization"] == "Bearer qq-token"
    assert client.kwargs["headers"]["X-QQ-User-ID"] == "456"


def test_group_schema_uses_astrbot_supported_types():
    schema = json.loads(Path(__file__).with_name("_conf_schema.json").read_text(encoding="utf-8"))
    assert schema["group_require_mention"]["type"] == "bool"
    assert schema["group_cooldown_seconds"]["type"] == "float"
    assert schema["group_max_replies_per_hour"]["type"] == "int"


def test_group_configuration_parsing_is_fail_closed():
    assert _MOD.normalize_assistant_mode("group") == "group"
    assert _MOD.normalize_assistant_mode("unexpected") == "personal"
    assert _MOD.parse_bool("invalid", default=True) is True
    assert _MOD.parse_group_allowed_ids("456, 789; 101112") == frozenset({"456", "789", "101112"})
    assert _MOD.parse_group_allowed_ids(["456", "not-a-group", "789"]) == frozenset({"456", "789"})


def test_personal_mode_keeps_group_silent():
    _MOD._GROUP_REPLY_TIMES.clear()
    client = _PostClient()
    plugin = _plugin(client)
    event = _Event([], "普通群消息", sender="123", group="456", self_id="999")

    asyncio.run(plugin.on_message(event))

    assert event.stopped is True
    assert event.llm_blocked is True
    assert event.sent == []
    assert client.kwargs is None


def test_group_mode_owner_uses_visitor_token_and_scope():
    _MOD._GROUP_REPLY_TIMES.clear()
    client = _PostClient()
    plugin = _plugin(client)
    plugin.cfg["assistant_mode"] = "group"
    event = _Event(
        [sys.modules["astrbot.api.message_components"].At("999")],
        "@小月 你好",
        sender="123",
        group="456",
        self_id="999",
    )

    asyncio.run(plugin.on_message(event))

    assert event.stopped is True
    assert event.sent
    assert client.kwargs["headers"]["Authorization"] == "Bearer qq-token"
    assert client.kwargs["headers"]["X-QQ-User-ID"] == "123"
    assert client.kwargs["json"]["group_id"] == "456"


def test_group_mode_requires_whitelist_and_mention():
    client = _PostClient()
    plugin = _plugin(client)
    plugin.cfg["assistant_mode"] = "group"

    not_mentioned = _Event([], "普通群消息", sender="456", group="456", self_id="999")
    asyncio.run(plugin.on_message(not_mentioned))
    assert not_mentioned.sent == []
    assert client.kwargs is None

    other_group = _Event(
        [sys.modules["astrbot.api.message_components"].At("999")],
        "@小月 你好",
        sender="456",
        group="789",
        self_id="999",
    )
    asyncio.run(plugin.on_message(other_group))
    assert other_group.sent == []
    assert client.kwargs is None


def test_group_rate_limit_is_per_group():
    _MOD._GROUP_REPLY_TIMES.clear()
    assert _MOD.group_rate_limited("456", cooldown_seconds=10, max_replies_per_hour=2, now=100) is False
    assert _MOD.group_rate_limited("456", cooldown_seconds=10, max_replies_per_hour=2, now=105) is True
    assert _MOD.group_rate_limited("789", cooldown_seconds=10, max_replies_per_hour=2, now=105) is False
    assert _MOD.group_rate_limited("456", cooldown_seconds=10, max_replies_per_hour=2, now=111) is False
    assert _MOD.group_rate_limited("456", cooldown_seconds=10, max_replies_per_hour=2, now=122) is True


def test_image_identification_and_caption_cleanup():
    comp = sys.modules["astrbot.api.message_components"].Image(url="https://cdn.invalid/a.png")
    event = _Event([comp], "请看看 [image] https://cdn.invalid/a.png")
    assert _MOD.XiaoYuePlugin._find_image_component(event) is comp
    assert _MOD.clean_image_caption(event.get_message_str()) == "请看看"


def test_group_image_is_stopped_before_upload():
    comp = sys.modules["astrbot.api.message_components"].Image(url="https://cdn.invalid/a.png")
    event = _Event([comp], "[image]", sender="123", group="456")
    plugin = _plugin()
    asyncio.run(plugin.on_message(event))
    assert event.stopped is True
    assert event.sent == []
    assert plugin._client.kwargs is None


def test_stream_download_size_limit_and_magic_validation(tmp_path):
    good = b"\x89PNG\r\n\x1a\n" + b"x" * 8
    target = tmp_path / "image.bin"
    plugin = _plugin(_StreamClient(_StreamResponse(good, content_length=len(good))))
    mime = asyncio.run(plugin._download_image("https://cdn.invalid/a.png", str(target)))
    assert mime == "image/png"
    assert target.read_bytes() == good

    too_large = tmp_path / "large.bin"
    plugin = _plugin(_StreamClient(_StreamResponse(good, content_length=20)), max_bytes=10)
    with pytest.raises(_MOD.ImageTooLargeError):
        asyncio.run(plugin._download_image("https://cdn.invalid/a.png", str(too_large)))
    assert not too_large.exists()

    bad = tmp_path / "bad.bin"
    plugin = _plugin(_StreamClient(_StreamResponse(b"<svg></svg>", content_type="image/png")))
    with pytest.raises(_MOD.ImageFormatError):
        asyncio.run(plugin._download_image("https://cdn.invalid/a.png", str(bad)))
    assert not bad.exists()


def test_vision_multipart_fields_and_temp_cleanup(tmp_path):
    source = tmp_path / "source.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x")
    client = _PostClient()
    plugin = _plugin(client)
    comp = sys.modules["astrbot.api.message_components"].Image(file=str(source), name="photo.png")
    event = _Event([comp], "看图 [image]", sender="456")
    asyncio.run(plugin._handle_image(event, comp, _MOD.clean_image_caption(event.get_message_str())))
    assert event.sent
    assert client.kwargs["data"]["message"] == "看图"
    assert client.kwargs["data"]["user_id"] == "456"
    assert client.kwargs["data"]["request_id"]
    assert client.kwargs["files"]["image"][2] == "image/png"
    assert client.kwargs["headers"]["X-QQ-Request-ID"] == client.kwargs["data"]["request_id"]
    assert source.exists()


def test_vision_http_error_has_short_format_message(tmp_path):
    request = httpx.Request("POST", "http://local/api/chat/vision")
    response = httpx.Response(415, request=request)
    client = _PostClient(_PostResponse(error=httpx.HTTPStatusError("bad", request=request, response=response)))
    plugin = _plugin(client)
    source = tmp_path / "source.jpg"
    source.write_bytes(b"\xff\xd8\xff" + b"x")
    comp = sys.modules["astrbot.api.message_components"].Image(file=str(source))
    event = _Event([comp], "[image]")
    asyncio.run(plugin._handle_image(event, comp, ""))
    assert "格式不支持" in event.sent[0][0].text
