"""聊天请求上下文层。

本模块只负责请求级数据：请求模型、认证后的用户身份、消息长度/访客限流，
以及 request_id 的单飞与成功结果缓存。它不执行命令、不检索数据，也不调用 LLM。
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import HTTPException, Request
from pydantic import BaseModel

from app.config import settings as default_settings


class ChatRequest(BaseModel):
    message: str
    # 客户端重试幂等键；同一用户同一 request_id 的成功响应只执行一次。
    request_id: str | None = None
    # QQ 插件透传的发送者 QQ 号。空 = 主人（桌面端/本地调用）。
    user_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    memories_used: int


@dataclass(frozen=True)
class ChatContext:
    """一轮聊天的不可变请求上下文。"""

    request: Request
    request_model: ChatRequest
    message: str
    uid: str
    is_owner: bool
    auth: Any | None = None

    @property
    def request_id(self) -> str | None:
        return self.request_model.request_id

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
_request_cache: dict[tuple[str, str], ChatResponse] = {}
_request_inflight: dict[tuple[str, str], asyncio.Task] = {}
_request_lock = asyncio.Lock()


def authenticated_uid(req: ChatRequest, request: Request, memory_module: Any) -> tuple[str, bool]:
    """按当前认证上下文解析用户身份，并拒绝跨角色冒充。"""
    auth = getattr(getattr(request, "state", None), "auth", None)
    if auth is not None and auth.role not in {"owner", "internal", "qq"}:
        raise HTTPException(status_code=403, detail="forbidden")

    if auth is not None and auth.role == "qq":
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
        # owner/internal token 不信任 body 中的 user_id。
        return memory_module.owner_user_id(), True

    try:
        uid = memory_module.normalize_user_id(req.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return uid, memory_module.is_owner_user(uid)


def build_context(req: ChatRequest, request: Request, memory_module: Any) -> ChatContext:
    """从 FastAPI 请求构造一轮聊天上下文。"""
    uid, is_owner = authenticated_uid(req, request, memory_module)
    return ChatContext(
        request=request,
        request_model=req,
        message=req.message.strip(),
        uid=uid,
        is_owner=is_owner,
        auth=getattr(getattr(request, "state", None), "auth", None),
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


async def deduplicate_request(
    req: ChatRequest,
    request: Request,
    memory_module: Any,
    handler: Callable[[ChatRequest, Request], Awaitable[ChatResponse]],
) -> ChatResponse:
    """对带 request_id 的请求做单飞与成功结果复用。

    失败不会写入缓存；并发请求共享同一 Task，避免重复执行副作用和 LLM 调用。
    """
    if not req.request_id:
        return await handler(req, request)

    uid, _ = authenticated_uid(req, request, memory_module)
    key = (uid, req.request_id[:128])
    async with _request_lock:
        cached = _request_cache.get(key)
        if cached is not None:
            return cached
        task = _request_inflight.get(key)
        if task is None:
            task = asyncio.create_task(handler(req, request))
            _request_inflight[key] = task

    try:
        result = await task
    except BaseException:
        async with _request_lock:
            _request_inflight.pop(key, None)
        raise

    async with _request_lock:
        _request_inflight.pop(key, None)
        _request_cache[key] = result
        if len(_request_cache) > _REQUEST_CACHE_MAX:
            _request_cache.pop(next(iter(_request_cache)))
    return result
