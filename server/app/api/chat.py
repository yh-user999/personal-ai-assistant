"""聊天 API 兼容层。

聊天业务已拆到 ``app.chat``：context 处理请求状态，routing 处理快捷命令，
retrieval 处理记忆/知识检索，prompting 负责提示词，pipeline 负责响应编排。
本文件只保留 API 路由、兼容导出和运行时依赖组装，以维持旧调用方与测试的导入路径。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.auth import require_roles
from app.chat.context import (
    ChatRequest,
    ChatResponse,
    ImagePayload,
    ChatRuntime,
    _guest_events,
    _request_cache,
    _request_inflight,
    authenticated_uid,
    build_context,
    computer_online,
    deduplicate_request,
    guest_rate_limited,
)
from app.chat.pipeline import run_chat
from app.chat.prompting import _GENERATION_INTENT, SYSTEM_PROMPT, _untrusted_reference
from app.chat.routing import (
    _COMMAND_HANDLERS,
    GUEST_BLOCKED_HANDLERS,
    parse_time_question,
)
from app.config import settings
from app.core import knowledge, llm, memory
from app.models.database import connect
from app.novel import NovelApplicationService
from app.services import (
    behavior_context,
    chapter_analysis,
    concern_tracker,
    confirm,
    cooccurrence,
    documents,
    executor,
    fact_extract,
    few_shot,
    fitness,
    goals,
    growth,
    identity_guard,
    index_healer,
    initiative,
    intent_goals,
    jargon,
    knowledge_domain,
    knowledge_hint,
    message_search,
    mood,
    novel_entities,
    novel_writing,
    plain_text,
    profile,
    reminders,
    request_trace,
    resume,
    sanitize,
    self_reflect,
    self_state,
    slang,
    subjective_time,
    unresolved,
    worklog,
    vision,
)

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
    """按当前 API 模块依赖创建运行时，确保 monkeypatch 实时生效。"""
    services = SimpleNamespace(
        behavior_context=behavior_context,
        chapter_analysis=chapter_analysis,
        cooccurrence=cooccurrence,
        confirm=confirm,
        concern_tracker=concern_tracker,
        documents=documents,
        executor=executor,
        fact_extract=fact_extract,
        fitness=fitness,
        few_shot=few_shot,
        goals=goals,
        growth=growth,
        identity_guard=identity_guard,
        index_healer=index_healer,
        initiative=initiative,
        intent_goals=intent_goals,
        jargon=jargon,
        knowledge_domain=knowledge_domain,
        knowledge_hint=knowledge_hint,
        message_search=message_search,
        mood=mood,
        novel_entities=novel_entities,
        novel_writing=novel_writing,
        plain_text=plain_text,
        profile=profile,
        reminders=reminders,
        request_trace=request_trace,
        resume=resume,
        sanitize=sanitize,
        self_reflect=self_reflect,
        self_state=self_state,
        slang=slang,
        subjective_time=subjective_time,
        unresolved=unresolved,
        worklog=worklog,
        novel=NovelApplicationService.from_legacy(novel_writing, chapter_analysis, novel_entities),
    )
    return ChatRuntime(
        settings=settings,
        llm=llm,
        memory=memory,
        knowledge=knowledge,
        services=services,
        bg_tasks=_bg_tasks,
        logger=__import__("logging").getLogger("assistant.chat"),
    )


def _authenticated_uid(req: ChatRequest, request: Request) -> tuple[str, bool]:
    """兼容旧 API 的认证辅助函数。"""
    return authenticated_uid(req, request, memory)


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
async def chat(req: ChatRequest, request: Request) -> ChatResponse:
    """聊天入口：对带 request_id 的客户端重试做单飞与结果复用。"""
    if req.image is not None:
        raise HTTPException(status_code=400, detail="图片提问请使用 multipart /api/chat/vision")
    return await deduplicate_request(req, request, memory, _chat_impl)


@router.post("/chat/vision", response_model=ChatResponse)
async def vision_chat(
    request: Request,
    message: str | None = Form(None),
    request_id: str | None = Form(None),
    user_id: str | None = Form(None),
    image: UploadFile | None = File(None),
) -> ChatResponse:
    """图片+文字提问：先在边界读取/校验图片，再复用认证、幂等和主聊天链路。"""
    if image is None:
        raise HTTPException(status_code=400, detail="缺少 image 图片文件")
    if not str(request_id or "").strip():
        raise HTTPException(status_code=400, detail="request_id 不能为空")
    validated = await vision.validate_upload(
        image,
        max_bytes=getattr(settings, "vision_max_image_bytes", vision.DEFAULT_MAX_IMAGE_BYTES),
    )
    req = ChatRequest(
        message=message or "",
        request_id=request_id,
        user_id=user_id,
        image=ImagePayload(**validated.__dict__),
    )
    return await deduplicate_request(req, request, memory, _chat_impl)


@router.get("/greeting")
async def greeting() -> dict:
    """个性化问候（面板打开时实时刷新）。"""
    from app.services.greeting import get_greeting

    return {"greeting": get_greeting()}


@router.get("/messages")
async def recent_messages(limit: int = 30) -> dict:
    """最近消息：面板入口只返回主人自己的消息。"""
    limit = max(1, min(limit, 200))
    clause, args = memory._user_scope(memory.owner_user_id())
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT id, sender, content, ts FROM memories WHERE {clause} "
            "ORDER BY id DESC LIMIT ?",
            (*args, limit),
        ).fetchall()
    finally:
        conn.close()
    return {"messages": [dict(row) for row in reversed(rows)]}


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
