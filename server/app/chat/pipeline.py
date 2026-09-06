"""聊天响应流水线。

本模块按既有业务顺序串联请求校验、状态更新、命令路由、检索、提示词、LLM 和
持久化。它是编排层，不定义命令解析规则，也不承载 API 路由/认证缓存细节。
"""
from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Awaitable, Callable

from openai import OpenAIError

from app.chat import prompting, retrieval, routing
from app.chat.context import (
    GUEST_MAX_MSG_CHARS,
    OWNER_MAX_MSG_CHARS,
    ChatContext,
    ChatResponse,
    ChatRuntime,
    guest_rate_limited,
)


def _novel_model(runtime: ChatRuntime) -> str | None:
    """生成档优先使用 LLM 模块经过启动检查的小说模型。"""
    getter = getattr(runtime.llm, "get_novel_model", None)
    if callable(getter):
        return getter()
    return getattr(runtime.settings, "novel_llm_model", None) or getattr(
        runtime.settings, "llm_model", None
    )


def _vision_model(runtime: ChatRuntime) -> str | None:
    getter = getattr(runtime.llm, "get_vision_model", None)
    if callable(getter):
        return getter()
    return getattr(runtime.settings, "vision_llm_model", None) or getattr(
        runtime.settings, "llm_model", None
    )


async def _call_llm_with_fallback(
    ctx: ChatContext,
    runtime: ChatRuntime,
    assembly: prompting.PromptAssembly,
) -> tuple[str | None, bool]:
    """调用主聊天 LLM，返回 ``(reply, generation_failed)``。

    长文两次失败时返回 ``(None, True)``，让上层直接返回友好错误而不把错误文本
    当成 assistant 正常回复写入记忆。
    """
    plain_text = runtime.services.plain_text
    llm = runtime.llm
    request_id = ctx.request_id or uuid.uuid4().hex
    llm_context = {"request_id": request_id, "user_id": ctx.uid}
    if ctx.image is not None:
        try:
            timeout = getattr(runtime.settings, "vision_timeout", 90.0)
            reply = (
                await llm.chat(
                    assembly.llm_messages,
                    timeout=timeout,
                    model=_vision_model(runtime),
                    **llm_context,
                )
            ).strip()
            if not reply:
                try:
                    ctx.request.state.chat_retryable_failure = True
                except AttributeError:
                    pass
                return None, False
            if plain_text.has_markdown(reply):
                reply = plain_text.strip_markdown(reply)
            return reply, False
        except Exception as exc:  # noqa: BLE001
            runtime.logger.warning("图片识别调用失败: %s", type(exc).__name__)
            try:
                ctx.request.state.chat_retryable_failure = True
            except AttributeError:
                pass
            return None, False
    try:
        if assembly.gen_profile:
            reply = (
                await llm.chat(
                    assembly.gen_messages,
                    timeout=240,
                    max_tokens=6000,
                    model=_novel_model(runtime),
                    **llm_context,
                )
            ).strip()
        else:
            reply = (await llm.chat(assembly.llm_messages, **llm_context)).strip()
        if plain_text.has_markdown(reply):
            runtime.logger.debug("回复含 Markdown，已转纯文本（%d 字）", len(reply))
            reply = plain_text.strip_markdown(reply)
        return reply, False
    except (OpenAIError, TimeoutError, RuntimeError, AttributeError):
        if not assembly.gen_profile:
            runtime.logger.exception("LLM 调用失败")
            return None, False
        try:
            runtime.logger.info("[gen] 首次生成失败，自动重试一次")
            reply = (
                await llm.chat(
                    assembly.gen_messages,
                    timeout=240,
                    max_tokens=6000,
                    model=_novel_model(runtime),
                    **llm_context,
                )
            ).strip()
            if plain_text.has_markdown(reply):
                reply = plain_text.strip_markdown(reply)
            runtime.logger.info("[gen] 重试成功，回复 %d 字", len(reply))
            return reply, False
        except (OpenAIError, TimeoutError, RuntimeError, AttributeError):
            runtime.logger.exception("[gen] 重试仍然失败")
            return None, True


async def _stream_llm_with_fallback(
    ctx: ChatContext,
    runtime: ChatRuntime,
    assembly: prompting.PromptAssembly,
    on_delta: Callable[[str], Awaitable[None]],
) -> tuple[str | None, bool]:
    """流式调用主聊天 LLM：增量经 ``on_delta`` 回调，返回 ``(全文, generation_failed)``。

    与全量版一致的策略：gen_profile 长文首次失败自动重试一次。但重试仅允许
    发生在**首个增量输出之前**——客户端已收到内容后换 Key 重发会造成重复
    输出，此时按失败处理，由上层下发友好错误。

    图片链路不走流式：调用方（/api/chat/stream）已在边界拒绝带图请求，
    这里兜底退化为全量调用后一次性回调。
    """
    plain_text = runtime.services.plain_text
    llm = runtime.llm
    request_id = ctx.request_id or uuid.uuid4().hex
    llm_context = {"request_id": request_id, "user_id": ctx.uid}

    if ctx.image is not None:
        reply, failed = await _call_llm_with_fallback(ctx, runtime, assembly)
        if reply:
            await on_delta(reply)
        return reply, failed

    messages = assembly.gen_messages if assembly.gen_profile else assembly.llm_messages
    kwargs: dict = dict(llm_context)
    if assembly.gen_profile:
        kwargs.update(timeout=240, max_tokens=6000, model=_novel_model(runtime))

    emitted = False

    async def pump() -> str:
        nonlocal emitted
        parts: list[str] = []
        async for delta in llm.chat_stream(messages, **kwargs):
            # 先标记再回调：回调即代表内容已下发客户端
            emitted = True
            await on_delta(delta)
            parts.append(delta)
        return "".join(parts)

    try:
        reply = (await pump()).strip()
    except (OpenAIError, TimeoutError, RuntimeError, AttributeError):
        if not assembly.gen_profile:
            runtime.logger.exception("LLM 流式调用失败")
            return None, False
        if emitted:
            # 客户端已收到部分增量，重试会造成重复输出，只能按失败收场
            runtime.logger.exception("[gen] 流式生成中途失败（已输出增量，不重试）")
            return None, True
        runtime.logger.info("[gen] 流式首次生成失败，自动重试一次")
        try:
            reply = (await pump()).strip()
            runtime.logger.info("[gen] 流式重试成功，回复 %d 字", len(reply))
        except (OpenAIError, TimeoutError, RuntimeError, AttributeError):
            runtime.logger.exception("[gen] 流式重试仍然失败")
            return None, True
    if plain_text.has_markdown(reply):
        runtime.logger.debug("回复含 Markdown，已转纯文本（%d 字）", len(reply))
        reply = plain_text.strip_markdown(reply)
    return reply, False


async def _record_request_trace(
    ctx: ChatContext,
    runtime: ChatRuntime,
    bundle: retrieval.RetrievalBundle,
    system: str,
) -> None:
    if not (ctx.is_owner and runtime.settings.request_trace_enabled):
        return
    trace = bundle.trace
    byte_sizes = {
        "knowledge": len(bundle.knowledge_text),
        "entity": len(bundle.entity_ctx),
        "healed": len(bundle.healed_text),
        "system_total": len(system),
        "original_query": trace.get("original_query", ctx.message),
        "search_query": trace.get("search_query", ctx.message),
        "anchors": trace.get("anchors", []),
        "expanded": bool(trace.get("expanded", False)),
    }
    runtime_task = asyncio.to_thread(
        runtime.services.request_trace.record,
        ctx.uid,
        ctx.message,
        trace["routing"],
        trace["path"],
        bool(trace["degraded"]),
        trace["healer_words"],
        byte_sizes,
        trace["search_ms"],
    )
    retrieval.track_background(runtime, runtime_task)


def _maybe_capture_chapter(
    assembly: prompting.PromptAssembly,
    reply: str,
    runtime: ChatRuntime,
    uid: str,
    request_id: str | None = None,
) -> None:
    """生成档长回复含"第X章"→ 后台自动提炼章节存档（被动抓取）。

    门槛：仅生成档（写第 N 章/续写意图）且回复 ≥1200 字且正文头部有章节号。
    失败由 capture_chapter_reply 内部静默，绝不影响主回复。
    用户从不打命令（写作台账/goals 同款教训），所以靠生成后自动留档。
    """
    if not (assembly.gen_profile and len(reply) >= 1200):
        return
    chapter_no = runtime.services.chapter_analysis.extract_chapter_no(reply)
    if not chapter_no:
        return
    capture = runtime.services.chapter_analysis.capture_chapter_reply
    try:
        parameters = inspect.signature(capture).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    capture_kwargs = {}
    if request_id is not None and ("request_id" in parameters or accepts_kwargs):
        capture_kwargs["request_id"] = request_id
    retrieval.track_background(
        runtime,
        capture(chapter_no, reply, uid, **capture_kwargs),
    )


async def run_chat(ctx: ChatContext, runtime: ChatRuntime) -> ChatResponse:
    """执行一轮普通聊天，命令命中时在 LLM 前短路返回。"""
    return await _run_chat(ctx, runtime, on_delta=None)


async def run_chat_stream(
    ctx: ChatContext,
    runtime: ChatRuntime,
    on_delta: Callable[[str], Awaitable[None]],
) -> ChatResponse:
    """流式版聊天：增量经 ``on_delta`` 逐段回调，返回值与 run_chat 一致。

    命令短路、限流、生成失败等一次性回复不会产生任何 delta，直接作为
    返回值交给调用方下发（SSE done 事件）。
    """
    return await _run_chat(ctx, runtime, on_delta=on_delta)


async def _run_chat(
    ctx: ChatContext,
    runtime: ChatRuntime,
    *,
    on_delta: Callable[[str], Awaitable[None]] | None,
) -> ChatResponse:
    """聊天主链路共享核心：``on_delta`` 为 None 走全量调用，否则走流式。"""
    settings = runtime.settings
    memory = runtime.memory
    services = runtime.services
    msg = ctx.message

    max_chars = OWNER_MAX_MSG_CHARS if ctx.is_owner else GUEST_MAX_MSG_CHARS
    if len(msg) > max_chars:
        return ChatResponse(
            reply=f"消息太长啦（{len(msg)} 字，上限 {max_chars}），精简一下再发",
            memories_used=0,
        )

    if ctx.image is None and ctx.is_owner and settings.healer_enabled:
        fixed = services.index_healer.apply_correction(msg)
        if fixed is not None:
            return ChatResponse(reply=fixed, memories_used=0)

    if not ctx.is_owner and guest_rate_limited(ctx.uid):
        window_minutes = 1
        return ChatResponse(
            reply=f"⏳ 聊得太快啦，歇 {window_minutes} 分钟再来吧",
            memories_used=0,
        )

    if ctx.is_owner:
        mood_name = services.mood.detect_mood_name(msg)
        if mood_name:
            # 同步 SQLite 迁出事件循环线程（prepare_turn/assemble 同理）
            await asyncio.to_thread(services.mood.record_mood, mood_name, msg, user_id=ctx.uid)
        if settings.initiative_enabled:
            if ctx.uid:
                services.initiative.mark_responded(user_id=ctx.uid)
            else:
                # 兼容旧测试替身；真实请求的 ChatContext 总有认证主体。
                services.initiative.mark_responded()

    routed = await routing.dispatch(ctx, runtime)
    if routed is not None:
        return routed

    # 黑话二期：链接+短句语境推断，仅主人，后台失败静默。
    if ctx.is_owner and settings.healer_enabled:
        history = await asyncio.to_thread(memory.get_recent_history, 4, user_id=ctx.uid)
        last_user = next(
            (item["content"] for item in reversed(history) if item["role"] == "user"),
            "",
        )
        if last_user and services.slang.detect_link_followup(last_user, msg):
            retrieval.track_background(
                runtime,
                services.slang.infer_candidate(
                    last_user, msg, user_id=ctx.uid, request_id=ctx.request_id
                ),
            )

    if services.unresolved.detect_resolved(msg):
        await asyncio.to_thread(services.unresolved.resolve_latest, user_id=ctx.uid)
    elif services.unresolved.detect_unresolved(msg):
        await asyncio.to_thread(services.unresolved.add_issue, msg, user_id=ctx.uid)

    # prepare_turn 保持在事件循环：它内部会调度后台任务（事实桥接），
    # 工作线程里没有可用的 loop；其 DB 开销仅一条小 SELECT + 罕见写入。
    preparation = retrieval.prepare_turn(ctx, runtime)
    bundle = await retrieval.retrieve(ctx, runtime, preparation)
    # assemble 的注入器均为纯查询（无任务调度），可安全移入工作线程
    assembly = await asyncio.to_thread(prompting.assemble, ctx, runtime, bundle)
    await _record_request_trace(ctx, runtime, bundle, assembly.system)

    memory_text = f"{msg}\n[图片]" if ctx.image is not None else msg
    await memory.write_message(
        "user",
        memory_text,
        user_id=ctx.uid,
        precomputed_vec=(
            None
            if ctx.image is not None
            else memory.take_query_vec(services.sanitize.sanitize(msg))
        ),
    )

    if on_delta is None:
        reply, generation_failed = await _call_llm_with_fallback(ctx, runtime, assembly)
    else:
        reply, generation_failed = await _stream_llm_with_fallback(ctx, runtime, assembly, on_delta)
    if reply is None:
        if ctx.image is not None:
            return ChatResponse(reply="抱歉，这张图片暂时识别失败，请稍后重试。", memories_used=0)
        return ChatResponse(
            reply=(
                "抱歉，长文生成连续两次失败（可能是服务商超时），等两分钟再说「继续」？"
                if generation_failed
                else "抱歉，我这会儿连不上大脑（LLM 调用失败），稍后再说一次？"
            ),
            memories_used=0,
        )

    await memory.write_message("assistant", reply, user_id=ctx.uid)
    if bundle.mems:
        await asyncio.to_thread(memory.bump_importance, [item["id"] for item in bundle.mems])
    if bundle.definition_term:
        await asyncio.to_thread(
            services.jargon.save_term, bundle.definition_term, reply, user_id=ctx.uid
        )

    retrieval.track_background(
        runtime,
        services.fact_extract.maybe_extract_facts(
            msg, user_id=ctx.uid, request_id=ctx.request_id
        ),
    )

    _maybe_capture_chapter(assembly, reply, runtime, ctx.uid, ctx.request_id)
    return ChatResponse(reply=reply, memories_used=len(bundle.mems))
