"""聊天 API 兼容层。

聊天业务已拆到 ``app.chat``：context 处理请求状态，routing 处理快捷命令，
retrieval 处理记忆/知识检索，prompting 负责提示词，pipeline 负责响应编排。
本文件只保留 API 路由、兼容导出和运行时依赖组装，以维持旧调用方与测试的导入路径。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from logging import getLogger

from fastapi import APIRouter, File, Form, Request, Response, UploadFile
from fastapi.responses import StreamingResponse

from app.api.errors import api_error
from app.auth import require_roles
from app.chat.context import (
    ChatRequest,
    ChatResponse,
    ImagePayload,
    ChatRuntime,
    _guest_events,
    _request_cache,
    _request_inflight,
    build_context,
    computer_online,
    deduplicate_request,
    guest_rate_limited,
)
from app.chat.pipeline import run_chat, run_chat_stream
from app.chat.services_registry import make_services
from app.services import message_search, mood, vision
from app.chat.prompting import _GENERATION_INTENT, SYSTEM_PROMPT, _untrusted_reference
from app.chat.routing import (
    _COMMAND_HANDLERS,
    GUEST_BLOCKED_HANDLERS,
    parse_time_question,
)
from app.config import settings
from app.core import knowledge, llm, memory
from app.models import repo

router = APIRouter()

__all__ = [
    "GUEST_BLOCKED_HANDLERS",
    "SYSTEM_PROMPT",
    "_COMMAND_HANDLERS",
    "_GENERATION_INTENT",
    "_guest_events",
    "_request_cache",
    "_request_inflight",
    "_untrusted_reference",
    "parse_time_question",
]


# 后台任务引用集：保持原模块级符号，兼容测试与外部调用方。
_bg_tasks = set()


def _build_runtime(request: Request | None = None) -> ChatRuntime:
    """按当前 API 模块依赖创建运行时；服务清单统一在 services_registry 维护。"""
    return ChatRuntime(
        settings=settings,
        llm=llm,
        memory=memory,
        knowledge=knowledge,
        services=make_services(),
        bg_tasks=_bg_tasks,
        logger=getLogger("assistant.chat"),
    )


def _guest_rate_limited(uid: str) -> bool:
    """兼容旧 API 的访客限流辅助函数。"""
    return guest_rate_limited(uid)


def _computer_online(hb: dict | None, stale_seconds: int | None = None) -> bool:
    """兼容旧 API 的采集器在线状态判断。"""
    return computer_online(hb, stale_seconds=stale_seconds)


async def _chat_impl(req: ChatRequest, request: Request) -> ChatResponse:
    """兼容旧调用方的主链路入口。"""
    ctx = build_context(req, request, memory)
    return await run_chat(ctx, _build_runtime(request))


@router.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    request: Request,
    response: Response = None,
) -> ChatResponse:
    """聊天入口：对带 request_id 的客户端重试做单飞与结果复用。"""
    if req.image is not None:
        raise api_error(400, "image_not_supported", "图片提问请使用 multipart /api/chat/vision")
    result = await deduplicate_request(req, request, memory, _chat_impl)
    trace_id = str(getattr(getattr(request, "state", None), "trace_id", "") or "")
    if trace_id and response is not None:
        response.headers["X-Trace-ID"] = trace_id
    return result


@router.post("/chat/vision", response_model=ChatResponse)
async def vision_chat(
    request: Request,
    response: Response = None,
    message: str | None = Form(None),
    request_id: str | None = Form(None),
    user_id: str | None = Form(None),
    group_id: str | None = Form(None),
    image: UploadFile | None = File(None),
) -> ChatResponse:
    """图片+文字提问：先在边界读取/校验图片，再复用认证、幂等和主聊天链路。"""
    if image is None:
        raise api_error(400, "image_missing", "缺少 image 图片文件")
    if not str(request_id or "").strip():
        raise api_error(400, "request_id_required", "request_id 不能为空")
    validated = await vision.validate_upload(
        image,
        max_bytes=settings.vision_max_image_bytes,
    )
    req = ChatRequest(
        message=message or "",
        request_id=request_id,
        user_id=user_id,
        group_id=group_id,
        image=ImagePayload(**validated.__dict__),
    )
    result = await deduplicate_request(req, request, memory, _chat_impl)
    trace_id = str(getattr(getattr(request, "state", None), "trace_id", "") or "")
    if trace_id:
        response.headers["X-Trace-ID"] = trace_id
    return result


@router.post("/chat/observe")
async def observe_group_message(req: ChatRequest, request: Request) -> dict:
    """只收录群消息：写入本群作用域并提取画像，不调用 LLM、不产生回复。

    用于"只收集不回复"的群：机器人在这些群完全沉默，但消息仍可长期检索。
    必须带 group_id——本端点不接受私聊，避免被用来绕过正常聊天链路
    往私聊记忆里写数据。
    """
    ctx = build_context(req, request, memory)
    if not ctx.is_group:
        raise api_error(400, "group_id_required", "只收录端点仅接受群消息")
    if not ctx.message:
        return {"stored": False, "reason": "empty"}

    memory_id = await memory.write_message(
        "user", ctx.message, user_id=ctx.uid, group_id=ctx.group_id
    )
    # 画像提取放后台：它要调 LLM，不能拖慢消息收录。
    if getattr(settings, "group_profile_enabled", True):
        from app.services import group_profile_extract

        _bg_tasks.add(
            task := asyncio.create_task(
                group_profile_extract.maybe_extract(
                    ctx.group_id, ctx.uid, ctx.message, request_id=ctx.request_id
                )
            )
        )
        task.add_done_callback(_bg_tasks.discard)
    return {"stored": memory_id is not None}


def _sse_frame(event: str, data: dict) -> str:
    """编码一条 SSE 事件帧（data 为 JSON，中文不转义以减少传输体积）。"""
    return "event: " + event + "\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n"


@router.post("/chat/stream")
async def chat_stream_api(req: ChatRequest, request: Request) -> StreamingResponse:
    """SSE 流式聊天入口，事件序列：meta → delta* → done（异常时 error）。

    与 /chat 共用认证、限流与主链路；差异：
    - 图片提问不支持流式，仍走 multipart /api/chat/vision；
    - 不进 deduplicate_request 的结果缓存（流式响应无法整体复用），
      request_id 仅用于链路追踪与 LLM 用量记账。
    done 携带服务端清洗后的最终全文，客户端以其覆盖累计增量。
    """
    if req.image is not None:
        raise api_error(400, "image_not_supported", "图片提问请使用 multipart /api/chat/vision")
    ctx = build_context(req, request, memory)
    runtime = _build_runtime(request)

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        async def on_delta(text: str) -> None:
            await queue.put(("delta", text))

        async def drive() -> None:
            try:
                resp = await run_chat_stream(ctx, runtime, on_delta)
                await queue.put(("done", resp))
            except Exception:
                runtime.logger.exception("流式聊天链路异常")
                await queue.put(("error", None))
            finally:
                await queue.put(("end", None))

        task = asyncio.create_task(drive())
        yield _sse_frame(
            "meta",
            {"request_id": ctx.request_id or "", "trace_id": ctx.trace.trace_id},
        )
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "end":
                    break
                if kind == "delta":
                    yield _sse_frame("delta", {"text": payload})
                elif kind == "done":
                    yield _sse_frame(
                        "done",
                        {"reply": payload.reply or "", "memories_used": payload.memories_used},
                    )
                elif kind == "error":
                    yield _sse_frame("error", {"message": "生成中断，请稍后重试"})
        finally:
            # 客户端断开时取消后台主链路，终止未完成的 LLM 流
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Trace-ID": ctx.trace.trace_id,
        },
    )


@router.get("/greeting")
async def greeting() -> dict:
    """个性化问候（面板打开时实时刷新）。"""
    from app.services.greeting import get_greeting

    return {"greeting": get_greeting()}


@router.get("/messages")
async def recent_messages(limit: int = 30) -> dict:
    """最近消息：面板入口只返回主人自己的消息。"""
    limit = max(1, min(limit, 200))
    rows = await asyncio.to_thread(
        repo.recent_memories, memory.owner_user_id(), limit
    )
    return {"messages": rows}


@router.get("/messages/search")
async def search_messages_api(q: str = "") -> dict:
    """消息全文搜索：移出事件循环，只搜索主人自己的消息。"""
    return await asyncio.to_thread(
        message_search.search_messages,
        q,
        message_search.MAX_HITS,
        memory.owner_user_id(),
    )


@router.get("/mood/state")
async def mood_state(request: Request) -> dict:
    """情绪状态：悬浮球轮询使用，仅返回认证主体数据。"""
    auth = require_roles(request, "owner", "internal")
    uid = str(auth.subject or memory.owner_user_id())
    return {
        "streak_active": bool(mood.get_streak_injection(user_id=uid)),
        "today_text": mood.get_today_injection(user_id=uid),
    }
