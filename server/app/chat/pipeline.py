"""聊天响应流水线。

本模块按既有业务顺序串联请求校验、状态更新、命令路由、检索、提示词、LLM 和
持久化。它是编排层，不定义命令解析规则，也不承载 API 路由/认证缓存细节。
"""
from __future__ import annotations

import asyncio

from app.chat.context import (
    GUEST_MAX_MSG_CHARS,
    GUEST_WINDOW_SECONDS,
    OWNER_MAX_MSG_CHARS,
    ChatContext,
    ChatResponse,
    ChatRuntime,
    guest_rate_limited,
)
from app.chat import prompting, retrieval, routing


async def _call_llm_with_fallback(
    runtime: ChatRuntime,
    assembly: prompting.PromptAssembly,
) -> tuple[str | None, bool]:
    """调用主聊天 LLM，返回 ``(reply, generation_failed)``。

    长文两次失败时返回 ``(None, True)``，让上层直接返回友好错误而不把错误文本
    当成 assistant 正常回复写入记忆。
    """
    plain_text = runtime.services.plain_text
    llm = runtime.llm
    try:
        if assembly.gen_profile:
            reply = (
                await llm.chat(assembly.gen_messages, timeout=240, max_tokens=6000)
            ).strip()
        else:
            reply = (await llm.chat(assembly.llm_messages)).strip()
        if plain_text.has_markdown(reply):
            runtime.logger.debug("回复含 Markdown，已转纯文本（%d 字）", len(reply))
            reply = plain_text.strip_markdown(reply)
        return reply, False
    except Exception:
        if not assembly.gen_profile:
            runtime.logger.exception("LLM 调用失败")
            return None, False
        try:
            runtime.logger.info("[gen] 首次生成失败，自动重试一次")
            reply = (
                await llm.chat(assembly.gen_messages, timeout=240, max_tokens=6000)
            ).strip()
            if plain_text.has_markdown(reply):
                reply = plain_text.strip_markdown(reply)
            runtime.logger.info("[gen] 重试成功，回复 %d 字", len(reply))
            return reply, False
        except Exception:
            runtime.logger.exception("[gen] 重试仍然失败")
            return None, True


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
    assembly: prompting.PromptAssembly, reply: str, runtime: ChatRuntime, uid: str
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
    retrieval.track_background(
        runtime,
        runtime.services.chapter_analysis.capture_chapter_reply(chapter_no, reply, uid),
    )


async def run_chat(ctx: ChatContext, runtime: ChatRuntime) -> ChatResponse:
    """执行一轮普通聊天，命令命中时在 LLM 前短路返回。"""
    settings = runtime.settings
    memory = runtime.memory
    services = runtime.services
    msg = ctx.message

    max_chars = (
        getattr(settings, "owner_max_msg_chars", OWNER_MAX_MSG_CHARS)
        if ctx.is_owner
        else getattr(settings, "guest_max_msg_chars", GUEST_MAX_MSG_CHARS)
    )
    if len(msg) > max_chars:
        return ChatResponse(
            reply=f"消息太长啦（{len(msg)} 字，上限 {max_chars}），精简一下再发",
            memories_used=0,
        )

    if ctx.is_owner and settings.healer_enabled:
        fixed = services.index_healer.apply_correction(msg)
        if fixed is not None:
            return ChatResponse(reply=fixed, memories_used=0)

    if not ctx.is_owner and guest_rate_limited(ctx.uid):
        window_minutes = 60 // 60 + 1
        return ChatResponse(
            reply=f"⏳ 聊得太快啦，歇 {window_minutes} 分钟再来吧",
            memories_used=0,
        )

    if ctx.is_owner:
        mood_name = services.mood.detect_mood_name(msg)
        if mood_name:
            services.mood.record_mood(mood_name, msg)
        if settings.initiative_enabled:
            services.initiative.mark_responded()

    routed = await routing.dispatch(ctx, runtime)
    if routed is not None:
        return routed

    # 黑话二期：链接+短句语境推断，仅主人，后台失败静默。
    if ctx.is_owner and settings.healer_enabled:
        history = memory.get_recent_history(4, user_id=ctx.uid)
        last_user = next(
            (item["content"] for item in reversed(history) if item["role"] == "user"),
            "",
        )
        if last_user and services.slang.detect_link_followup(last_user, msg):
            retrieval.track_background(
                runtime,
                services.slang.infer_candidate(last_user, msg, user_id=ctx.uid),
            )

    if services.unresolved.detect_resolved(msg):
        services.unresolved.resolve_latest(user_id=ctx.uid)
    elif services.unresolved.detect_unresolved(msg):
        services.unresolved.add_issue(msg, user_id=ctx.uid)

    preparation = retrieval.prepare_turn(ctx, runtime)
    bundle = await retrieval.retrieve(ctx, runtime, preparation)
    assembly = prompting.assemble(ctx, runtime, bundle)
    await _record_request_trace(ctx, runtime, bundle, assembly.system)

    await memory.write_message(
        "user",
        msg,
        user_id=ctx.uid,
        precomputed_vec=memory.take_query_vec(services.sanitize.sanitize(msg)),
    )

    reply, generation_failed = await _call_llm_with_fallback(runtime, assembly)
    if reply is None:
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
        memory.bump_importance([item["id"] for item in bundle.mems])
    if bundle.definition_term:
        services.jargon.save_term(bundle.definition_term, reply, user_id=ctx.uid)

    retrieval.track_background(
        runtime,
        services.fact_extract.maybe_extract_facts(msg, user_id=ctx.uid),
    )

    _maybe_capture_chapter(assembly, reply, runtime, ctx.uid)
    return ChatResponse(reply=reply, memories_used=len(bundle.mems))
