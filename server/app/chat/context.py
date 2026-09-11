"""聊天请求上下文层。

本模块只负责请求级数据：请求模型、认证后的用户身份、消息长度/访客限流，
以及 request_id 的单飞与成功结果缓存。它不执行命令、不检索数据，也不调用 LLM。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel

from app.config import settings as default_settings

logger = logging.getLogger("assistant.chat.context")


class ImagePayload(BaseModel):
    """已在 API 边界完成校验的图片；原始字节只存在于内存 data URL。"""

    media_type: str
    sha256: str
    size: int
    data_url: str


class ChatRequest(BaseModel):
    message: str
    # 客户端重试幂等键；同一用户同一 request_id 的成功响应只执行一次。
    request_id: str | None = None
    # QQ 插件透传的发送者 QQ 号。空 = 主人（桌面端/本地调用）。
    user_id: str | None = None
    # 图片只允许由 multipart API 注入，纯 JSON 请求仍保持兼容。
    image: ImagePayload | None = None


class ChatResponse(BaseModel):
    reply: str
    memories_used: int


@dataclass
class TraceContext:
    """一轮请求共享的本地 Trace 状态；只保存统计元数据，不保存 prompt/图片原文。"""

    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    request_id: str = ""
    channel: str = "chat"
    route_name: str = "/api/chat"
    started_at: float = field(default_factory=time.monotonic)
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    retrieval: dict[str, Any] = field(default_factory=dict)
    retrieval_trace: dict[str, Any] = field(default_factory=dict)
    injection_bytes: dict[str, Any] = field(default_factory=dict)
    reflection: dict[str, Any] = field(default_factory=dict)
    response_plan: dict[str, Any] = field(default_factory=dict)
    investigation_context: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"
    error_code: str = ""

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        started = time.monotonic()
        state = self.stages.setdefault(name, {})
        state["status"] = "running"
        try:
            yield
        except asyncio.CancelledError:
            state.update(status="cancelled", error="CancelledError")
            self.status = "failed"
            self.error_code = self.error_code or "CancelledError"
            raise
        except Exception as exc:
            state.update(status="failed", error=type(exc).__name__)
            self.status = "failed"
            self.error_code = self.error_code or type(exc).__name__
            raise
        else:
            state.update(status="ok", elapsed_ms=max(0, int((time.monotonic() - started) * 1000)))

    def fail(self, error_code: str) -> None:
        self.status = "failed"
        self.error_code = str(error_code or "error")[:80]

    @property
    def total_latency_ms(self) -> int:
        return max(0, int((time.monotonic() - self.started_at) * 1000))


@dataclass(frozen=True)
class ChatContext:
    """一轮聊天的不可变请求上下文。"""

    request: Request
    request_model: ChatRequest
    message: str
    uid: str
    is_owner: bool
    auth: Any | None = None
    image: ImagePayload | None = None
    trace: TraceContext = field(default_factory=TraceContext)

    @property
    def request_id(self) -> str | None:
        return self.request_model.request_id or self.trace.request_id or None

    def as_legacy_dict(self) -> dict[str, Any]:
        """为旧命令 handler/外部调用方提供原来的轻量 ctx 形态。"""
        return {"uid": self.uid, "is_owner": self.is_owner}


@dataclass(frozen=True)
class ChatRuntime:
    """聊天流水线显式依赖集合。

    ``services`` 是由兼容层组装的命名空间，里面放置各业务 service 模块。
    运行时对象按请求创建，因此测试可以替换 API 层暴露的依赖而不被模块缓存绑死。
    """

    settings: Any
    llm: Any
    memory: Any
    knowledge: Any
    services: Any
    bg_tasks: set[asyncio.Task]
    logger: Any


@dataclass(frozen=True)
class _CachedResponse:
    request_hash: str
    response: ChatResponse


# ── 访客限流 ───────────────────────────────────────────────
GUEST_WINDOW_SECONDS = 60
GUEST_WINDOW_LIMIT = 10
GUEST_DAY_LIMIT = 300
GUEST_MAX_MSGS_TRACKED = 2000
GUEST_MAX_MSG_CHARS = 2000
OWNER_MAX_MSG_CHARS = 8000

_guest_events: dict[str, deque[float]] = {}


# ── request_id 单飞与成功缓存 ─────────────────────────────
_REQUEST_CACHE_MAX = 256
_request_cache: dict[tuple[str, str], _CachedResponse] = {}
_request_inflight: dict[tuple[str, str], asyncio.Task] = {}
_request_inflight_hash: dict[tuple[str, str], str] = {}
_request_lock = asyncio.Lock()


def authenticated_uid(req: ChatRequest, request: Request, memory_module: Any) -> tuple[str, bool]:
    """按当前认证上下文解析用户身份，并拒绝跨角色冒充。"""
    auth = getattr(getattr(request, "state", None), "auth", None)
    if auth is not None and auth.role not in {"owner", "internal", "qq"}:
        raise HTTPException(status_code=403, detail="forbidden")

    if auth is not None and auth.role == "qq":
        # 真实 HTTP 请求的 subject 由 AuthMiddleware 根据 HMAC 签名头写入；
        # body.user_id 只能做一致性校验，不能作为身份来源。保留无 middleware
        # 的直接调用兼容分支，避免旧的内部测试/调用方无法构造 Request。
        signed_uid = str(getattr(auth, "subject", "") or "").strip()
        if signed_uid:
            signed_request_id = str(getattr(request.state, "qq_request_id", "") or "").strip()
            if signed_request_id and (req.request_id or "").strip() != signed_request_id:
                raise HTTPException(status_code=403, detail="qq request_id does not match signature")
            if memory_module.is_owner_user(signed_uid):
                raise HTTPException(status_code=403, detail="qq token cannot impersonate owner")
            if req.user_id is not None and str(req.user_id).strip():
                try:
                    body_uid = memory_module.normalize_user_id(req.user_id)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                if body_uid != signed_uid:
                    raise HTTPException(status_code=403, detail="qq identity does not match request body")
            return signed_uid, False

        raw_uid = str(req.user_id or "").strip()
        if raw_uid.casefold() == "owner" or raw_uid == memory_module.owner_user_id():
            raise HTTPException(status_code=403, detail="qq token cannot impersonate owner")
        try:
            uid = memory_module.normalize_user_id(req.user_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if memory_module.is_owner_user(uid):
            raise HTTPException(status_code=403, detail="qq token cannot impersonate owner")
        return uid, False

    if auth is not None:
        # owner/internal token 不信任 body 中的 user_id，subject 缺失时仍按当前
        # 配置解析主人，兼容旧的测试 Request 对象。
        return str(getattr(auth, "subject", "") or memory_module.owner_user_id()), True

    try:
        uid = memory_module.normalize_user_id(req.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return uid, memory_module.is_owner_user(uid)


def build_context(req: ChatRequest, request: Request, memory_module: Any) -> ChatContext:
    """从 FastAPI 请求构造一轮聊天上下文，并建立服务端 trace_id。"""
    uid, is_owner = authenticated_uid(req, request, memory_module)
    state = getattr(request, "state", None)
    auth = getattr(state, "auth", None)
    url = getattr(request, "url", None)
    route_name = str(getattr(url, "path", "") or "/api/chat")[:160]
    headers = getattr(request, "headers", {})
    header_request_id = ""
    try:
        header_request_id = str(headers.get("x-request-id", "") or "").strip()
    except AttributeError:
        header_request_id = ""
    request_id = str(req.request_id or header_request_id).strip()[:160]
    channel = str(getattr(state, "trace_channel", "") or "").strip()
    if not channel:
        if req.image is not None:
            channel = "vision"
        elif route_name.endswith("/stream"):
            channel = "stream"
        elif getattr(auth, "role", "") == "qq":
            channel = "qq"
        else:
            channel = "chat"
    trace = TraceContext(
        request_id=request_id,
        channel=channel[:40],
        route_name=route_name,
    )
    if state is not None:
        try:
            state.trace_id = trace.trace_id
        except AttributeError:
            pass
    return ChatContext(
        request=request,
        request_model=req,
        message=req.message.strip(),
        uid=uid,
        is_owner=is_owner,
        auth=auth,
        image=req.image,
        trace=trace,
    )


def guest_rate_limited(uid: str) -> bool:
    """记录本次访客访问并判定是否超限。返回 True = 应拒绝。"""
    now = time.time()
    events = _guest_events.get(uid)
    if events is None:
        if len(_guest_events) >= GUEST_MAX_MSGS_TRACKED:
            _guest_events.pop(next(iter(_guest_events)))
        events = deque()
        _guest_events[uid] = events

    while events and now - events[0] > 86400:
        events.popleft()
    if len(events) >= GUEST_DAY_LIMIT:
        return True
    recent = sum(1 for timestamp in events if now - timestamp <= GUEST_WINDOW_SECONDS)
    if recent >= GUEST_WINDOW_LIMIT:
        return True
    events.append(now)
    return False


def computer_online(hb: dict | None, stale_seconds: int | None = None) -> bool:
    """采集器心跳新鲜 = 电脑在线。"""
    if stale_seconds is None:
        stale_seconds = default_settings.heartbeat_stale_seconds
    if not hb or not hb.get("received_at"):
        return False
    try:
        timestamp = datetime.fromisoformat(hb["received_at"])
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - timestamp).total_seconds() < stale_seconds
    except (ValueError, TypeError):
        return False


def _request_hash(req: ChatRequest) -> str:
    """生成不包含 request_id 的规范化请求摘要（图片只记录摘要元数据）。"""
    image = req.image
    payload = {
        "message": req.message or "",
        "image_sha256": image.sha256 if image is not None else None,
        "image_media_type": image.media_type if image is not None else None,
        "image_size": image.size if image is not None else None,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _retryable_chat_failure(request: Request) -> bool:
    return bool(getattr(getattr(request, "state", None), "chat_retryable_failure", False))


async def _run_persistent_dedup(
    req: ChatRequest,
    request: Request,
    memory_module: Any,
    handler: Callable[[ChatRequest, Request], Awaitable[ChatResponse]],
    uid: str,
    request_id: str,
    request_hash: str,
) -> ChatResponse:
    """执行数据库级幂等；未初始化旧测试库时退回内存兼容路径。"""
    from app.services import request_dedup

    try:
        # 每次实际执行前清除上次尝试的失败标记，成功重试可正常缓存。
        try:
            request.state.chat_retryable_failure = False
        except AttributeError:
            pass
        claim = await asyncio.to_thread(
            request_dedup.claim,
            uid,
            request_id,
            request_hash,
        )
    except request_dedup.RequestDedupUnavailable:
        # app lifespan 会先 init_db；这里仅为直接调用旧内部 API/单测保留兼容。
        return await handler(req, request)

    if claim.state == "conflict":
        raise HTTPException(status_code=409, detail="request_id 已用于另一条消息")
    if claim.state == "completed":
        try:
            return request_dedup.decode_response(claim.response_json, ChatResponse)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.error("聊天幂等响应损坏，拒绝静默重复执行: %s", exc)
            raise HTTPException(status_code=500, detail="stored chat response is invalid") from exc
    if claim.state == "processing":
        response_json = await asyncio.to_thread(
            request_dedup.wait_for_completion,
            uid,
            request_id,
            request_hash,
        )
        if response_json:
            try:
                return request_dedup.decode_response(response_json, ChatResponse)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise HTTPException(status_code=500, detail="stored chat response is invalid") from exc
        raise HTTPException(
            status_code=409,
            detail="request_id 正在处理中，请稍后重试",
            headers={"Retry-After": "1"},
        )

    try:
        result = await handler(req, request)
    except BaseException:
        try:
            await asyncio.to_thread(
                request_dedup.release,
                uid,
                request_id,
                request_hash,
                claim.lease_expires_at,
            )
        except Exception:
            logger.warning("聊天请求失败后的幂等记录释放失败", exc_info=True)
        raise

    if _retryable_chat_failure(request):
        try:
            await asyncio.to_thread(
                request_dedup.release,
                uid,
                request_id,
                request_hash,
                claim.lease_expires_at,
            )
        except Exception:
            logger.warning("可重试聊天失败后的幂等记录释放失败", exc_info=True)
        return result

    try:
        await asyncio.to_thread(
            request_dedup.complete,
            uid,
            request_id,
            request_hash,
            claim.lease_expires_at,
            request_dedup.encode_response(result),
        )
    except request_dedup.RequestDedupUnavailable:
        logger.warning("聊天响应完成但幂等表不可用")
    except Exception:
        # 不吞主回复；记录会保持 processing，租约到期后可被接管。
        logger.warning("聊天响应持久化失败，保留租约等待接管", exc_info=True)
    return result


async def deduplicate_request(
    req: ChatRequest,
    request: Request,
    memory_module: Any,
    handler: Callable[[ChatRequest, Request], Awaitable[ChatResponse]],
) -> ChatResponse:
    """对带 request_id 的请求做进程内单飞和数据库级结果复用。

    相同主体的同一 request_id 若消息不同返回 409；业务失败不缓存，且释放
    数据库租约，客户端可安全重试。没有 request_id 的旧客户端保持原语义。
    """
    request_id = str(req.request_id or "").strip()
    if not request_id:
        return await handler(req, request)
    if len(request_id) > 128:
        raise HTTPException(status_code=400, detail="request_id 过长")

    uid, _ = authenticated_uid(req, request, memory_module)
    key = (uid, request_id)
    request_hash = _request_hash(req)
    async with _request_lock:
        cached = _request_cache.get(key)
        if cached is not None:
            if cached.request_hash != request_hash:
                raise HTTPException(status_code=409, detail="request_id 已用于另一条消息")
            return cached.response
        task = _request_inflight.get(key)
        if task is not None:
            if _request_inflight_hash.get(key) != request_hash:
                raise HTTPException(status_code=409, detail="request_id 已用于另一条消息")
        else:
            task = asyncio.create_task(
                _run_persistent_dedup(
                    req,
                    request,
                    memory_module,
                    handler,
                    uid,
                    request_id,
                    request_hash,
                )
            )
            _request_inflight[key] = task
            _request_inflight_hash[key] = request_hash

    try:
        result = await task
    except BaseException:
        async with _request_lock:
            _request_inflight.pop(key, None)
            _request_inflight_hash.pop(key, None)
        raise

    async with _request_lock:
        _request_inflight.pop(key, None)
        _request_inflight_hash.pop(key, None)
        if not _retryable_chat_failure(request):
            _request_cache[key] = _CachedResponse(request_hash, result)
            if len(_request_cache) > _REQUEST_CACHE_MAX:
                _request_cache.pop(next(iter(_request_cache)))
    return result
