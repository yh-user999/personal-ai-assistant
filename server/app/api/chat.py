"""聊天 API 兼容层。

聊天业务已拆到 ``app.chat``：context 处理请求状态，routing 处理快捷命令，
retrieval 处理记忆/知识检索，prompting 负责提示词，pipeline 负责响应编排。
本文件只保留 API 路由、兼容导出和运行时依赖组装，以维持旧调用方与测试的导入路径。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import APIRouter, Request

from app.auth import get_auth, require_roles
from app.config import settings
from app.core import knowledge, llm, memory
from app.models.database import connect
from app.novel import NovelApplicationService
from app.services import (
    behavior_context,
    chapter_analysis,
    cooccurrence,
    confirm,
    concern_tracker,
    documents,
    executor,
    fact_extract,
    fitness,
    few_shot,
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
)

from app.chat.context import (
    GUEST_DAY_LIMIT,
    GUEST_MAX_MSGS_TRACKED,
    GUEST_WINDOW_LIMIT,
    GUEST_WINDOW_SECONDS,
    OWNER_MAX_MSG_CHARS,
    GUEST_MAX_MSG_CHARS,
    ChatContext,
    ChatRequest,
    ChatResponse,
    ChatRuntime,
    _REQUEST_CACHE_MAX,
    _guest_events,
    _request_cache,
    _request_inflight,
    _request_lock,
    authenticated_uid,
    build_context,
    computer_online,
    deduplicate_request,
    guest_rate_limited,
)
from app.chat.pipeline import run_chat
from app.chat.prompting import (
    SYSTEM_PROMPT,
    _GENERATION_INTENT,
    _guest_note,
    _intent_rules_text,
    _untrusted_reference,
)
from app.chat.routing import (
    GUEST_BLOCKED_HANDLERS,
    _COMMAND_HANDLERS,
    _enqueue_and_reply,
    _handle_confirm,
    _handle_documents,
    _handle_entity_candidates,
    _handle_executor,
    _handle_fitness,
    _handle_goals,
    _handle_identity,
    _handle_novel,
    _handle_reminders,
    _handle_resume,
    _handle_search,
    _handle_slang,
    _handle_time,
    _handle_worklog,
    parse_time_question,
)


router = APIRouter()


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
async def mood_state() -> dict:
    """情绪状态：悬浮球轮询使用。"""
    return {
        "streak_active": bool(mood.get_streak_injection()),
        "today_text": mood.get_today_injection(),
    }
