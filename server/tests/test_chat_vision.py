"""图片聊天一期关键边界测试（不访问真实 LLM/网络）。"""
import asyncio

import pytest

from app.chat import prompting, routing
from app.chat.context import (
    ChatContext,
    ChatRequest,
    ChatResponse,
    ChatRuntime,
    ImagePayload,
    _request_hash,
    deduplicate_request,
)
from app.chat.retrieval import RetrievalBundle, IMAGE_SEARCH_PLACEHOLDER
from app.config import settings
from app.core import llm
from app.services import request_dedup
from app.services.vision import validate_upload


class _Upload:
    def __init__(self, data: bytes, content_type: str):
        self.data = data
        self.content_type = content_type

    async def read(self, size: int = -1):
        return self.data[:size]


def _jpeg():
    return b"\xff\xd8\xff\xe0\x00\x02\xff\xd9"


def _png():
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0dIHDR"
        + b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
        + b"\x90wS\xde"
        + b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _webp():
    body = b"WEBPVP8 " + (4).to_bytes(4, "little") + b"\x00" * 4
    return b"RIFF" + (len(body)).to_bytes(4, "little") + body


def _ctx(message: str, image: ImagePayload | None = None):
    return ChatContext(
        request=type("Request", (), {"state": type("State", (), {})()})(),
        request_model=ChatRequest(message=message, image=image),
        message=message,
        uid="owner",
        is_owner=True,
        image=image,
    )


@pytest.mark.parametrize(
    ("data", "content_type"),
    [(_jpeg(), "image/jpeg"), (_png(), "image/png"), (_webp(), "image/webp")],
)
def test_valid_image_payloads_are_memory_only(data, content_type):
    payload = asyncio.run(validate_upload(_Upload(data, content_type)))
    assert payload.media_type == content_type
    assert payload.size == len(data)
    assert payload.sha256
    assert payload.data_url.startswith(f"data:{content_type};base64,")


def test_image_validation_rejects_limit_mime_mismatch_svg_and_corruption():
    with pytest.raises(Exception) as exc:
        asyncio.run(validate_upload(_Upload(_jpeg() + b"x", "image/png")))
    assert exc.value.status_code == 415

    with pytest.raises(Exception) as exc:
        asyncio.run(validate_upload(_Upload(b"<svg></svg>", "image/svg+xml")))
    assert exc.value.status_code == 415

    with pytest.raises(Exception) as exc:
        asyncio.run(validate_upload(_Upload(b"not-image", "image/jpeg")))
    assert exc.value.status_code == 400

    with pytest.raises(Exception) as exc:
        asyncio.run(validate_upload(_Upload(_jpeg() + b"x", "image/jpeg"), max_bytes=len(_jpeg())))
    assert exc.value.status_code == 413


def test_request_dedup_hash_includes_image_sha256():
    first = ImagePayload(media_type="image/jpeg", sha256="a" * 64, size=10, data_url="data:image/jpeg;base64,eA==")
    second = first.model_copy(update={"sha256": "b" * 64})
    assert _request_hash(ChatRequest(message="看图", image=first)) != _request_hash(
        ChatRequest(message="看图", image=second)
    )


def test_vision_failure_is_not_cached(monkeypatch):
    monkeypatch.setattr(
        request_dedup,
        "claim",
        lambda *args, **kwargs: (_ for _ in ()).throw(request_dedup.RequestDedupUnavailable()),
    )
    state = type("State", (), {})()
    request = type("Request", (), {"state": state})()
    calls = []

    async def handler(req, _request):
        calls.append(1)
        if len(calls) == 1:
            request.state.chat_retryable_failure = True
            return ChatResponse(reply="识别失败", memories_used=0)
        return ChatResponse(reply="识别成功", memories_used=0)

    async def run():
        req = ChatRequest(message="", request_id="vision-retry")
        first = await deduplicate_request(req, request, type("Memory", (), {"normalize_user_id": staticmethod(lambda _: "owner"), "owner_user_id": staticmethod(lambda: "owner"), "is_owner_user": staticmethod(lambda uid: uid == "owner")})(), handler)
        second = await deduplicate_request(req, request, type("Memory", (), {"normalize_user_id": staticmethod(lambda _: "owner"), "owner_user_id": staticmethod(lambda: "owner"), "is_owner_user": staticmethod(lambda uid: uid == "owner")})(), handler)
        return first, second

    first, second = asyncio.run(run())
    assert first.reply == "识别失败" and second.reply == "识别成功"
    assert len(calls) == 2


def test_image_prompt_caption_and_default_instruction():
    image = ImagePayload(media_type="image/png", sha256="a" * 64, size=1, data_url="data:image/png;base64,eA==")
    bundle = RetrievalBundle()
    captioned = prompting.build_messages(_ctx("这是什么", image), bundle, "SYS")
    content = captioned.llm_messages[-1]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "这是什么"}
    assert content[1]["image_url"]["url"].startswith("data:image/png")
    assert captioned.gen_profile is False

    blank = prompting.build_messages(_ctx("", image), bundle, "SYS")
    assert prompting.DEFAULT_VISION_INSTRUCTION in blank.llm_messages[-1]["content"][0]["text"]


def test_image_query_has_non_empty_fixed_placeholder():
    assert IMAGE_SEARCH_PLACEHOLDER


def test_image_dispatch_bypasses_zero_llm_commands():
    image = ImagePayload(media_type="image/jpeg", sha256="a" * 64, size=1, data_url="data:image/jpeg;base64,eA==")
    ctx = _ctx("几点了", image)
    runtime = ChatRuntime(settings, None, None, None, None, set(), None)
    assert asyncio.run(routing.dispatch(ctx, runtime)) is None


def test_vision_model_is_separate_from_normal_model(monkeypatch):
    monkeypatch.setattr(settings, "llm_model", "normal-model")
    monkeypatch.setattr(settings, "vision_llm_model", "vision-model")
    assert llm.get_vision_model() == "vision-model"


def test_image_hash_ignores_ephemeral_data_url():
    first = ImagePayload(
        media_type="image/jpeg",
        sha256="a" * 64,
        size=10,
        data_url="data:image/jpeg;base64,first",
    )
    second = first.model_copy(update={"data_url": "data:image/jpeg;base64,second"})
    assert _request_hash(ChatRequest(message="看图", image=first)) == _request_hash(
        ChatRequest(message="看图", image=second)
    )


def test_vision_llm_receives_multimodal_content_and_request_context(monkeypatch):
    from types import SimpleNamespace

    from app.chat import pipeline

    image = ImagePayload(
        media_type="image/png",
        sha256="a" * 64,
        size=10,
        data_url="data:image/png;base64,raw-image-bytes",
    )
    captured = {}

    class FakeLLM:
        async def chat(self, messages, **kwargs):
            captured["messages"] = messages
            captured["kwargs"] = kwargs
            return "图片识别结果"

    runtime = ChatRuntime(
        settings,
        FakeLLM(),
        None,
        None,
        SimpleNamespace(
            plain_text=SimpleNamespace(
                has_markdown=lambda _text: False,
                strip_markdown=lambda text: text,
            )
        ),
        set(),
        __import__("logging").getLogger("test-vision"),
    )
    ctx = _ctx("这是什么？", image)
    ctx.request_model.request_id = "vision-request-1"
    assembly = prompting.build_messages(ctx, RetrievalBundle(), "system")
    reply, failed = asyncio.run(pipeline._call_llm_with_fallback(ctx, runtime, assembly))

    assert reply == "图片识别结果"
    assert failed is False
    assert captured["messages"][-1]["content"][1]["image_url"]["url"].endswith("raw-image-bytes")
    assert captured["kwargs"] == {
        "timeout": settings.vision_timeout,
        "model": settings.vision_llm_model,
        "request_id": "vision-request-1",
        "user_id": "owner",
    }


def test_vision_endpoint_injects_validated_image(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.api import chat as chat_api
    from app.main import app
    from app.models.database import reset_connections

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "vision-endpoint.db"))
    for name in (
        "api_token",
        "owner_api_token",
        "internal_api_token",
        "collector_api_token",
        "executor_api_token",
        "qq_api_token",
    ):
        monkeypatch.setattr(settings, name, "")
    reset_connections()
    captured = {}

    async def fake_deduplicate(req, request, memory_module, handler):
        captured["request"] = req
        return ChatResponse(reply="ok", memories_used=0)

    monkeypatch.setattr(chat_api, "deduplicate_request", fake_deduplicate)
    with TestClient(app) as client:
        response = client.post(
            "/api/chat/vision",
            data={"message": "请描述", "request_id": "vision-endpoint-1", "user_id": "10086"},
            files={"image": ("sample.png", _png(), "image/png")},
        )

    assert response.status_code == 200
    req = captured["request"]
    assert req.message == "请描述"
    assert req.request_id == "vision-endpoint-1"
    assert req.user_id == "10086"
    assert req.image.media_type == "image/png"
    assert req.image.sha256
    assert "raw" not in req.image.data_url


def test_vision_endpoint_requires_nonempty_request_id(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models.database import reset_connections

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "vision-empty-id.db"))
    for name in (
        "api_token",
        "owner_api_token",
        "internal_api_token",
        "collector_api_token",
        "executor_api_token",
        "qq_api_token",
    ):
        monkeypatch.setattr(settings, name, "")
    reset_connections()
    with TestClient(app) as client:
        response = client.post(
            "/api/chat/vision",
            data={"message": "", "request_id": "   "},
            files={"image": ("sample.png", _png(), "image/png")},
        )
    assert response.status_code == 400
    assert "request_id" in response.json()["detail"]


def test_signed_qq_request_id_must_match_multipart_form():
    from app.auth import AuthContext

    state = type("State", (), {})()
    state.auth = AuthContext("qq-token", "qq", "10086")
    state.qq_request_id = "signed-request"
    request = type("Request", (), {"state": state})()

    with pytest.raises(Exception) as exc:
        from app.chat.context import authenticated_uid
        from app.core import memory

        authenticated_uid(
            ChatRequest(message="", request_id="", user_id="10086"),
            request,
            memory,
        )
    assert exc.value.status_code == 403
