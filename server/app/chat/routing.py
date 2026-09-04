"""聊天命令路由层。

本模块只负责识别并执行零 LLM 的快捷命令及其确认流程。命令按历史注册顺序
短路返回；未命中时交回主流水线。业务 service 通过 ChatRuntime 注入，旧的
``(msg, request, ctx)`` handler 调用形态仍保留给现有测试和外部调用方。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from app.chat.context import ChatContext, ChatRequest, ChatResponse, ChatRuntime, computer_online


TIME_QUESTION = re.compile(r"几点了|现在几点|今天星期几|今天几号|今天几月几号|今天日期|现在时间|什么时间了")
TZ = ZoneInfo("Asia/Shanghai")

GUEST_BLOCKED_HANDLERS = frozenset(
    {
        "identity",
        "confirm",
        "worklog",
        "reminders",
        "documents",
        "resume",
        "fitness",
        "novel",
        "entity_candidates",
        "executor",
    }
)


def _coerce_context(msg: str, request: Any, ctx: ChatContext | dict | None) -> ChatContext:
    if isinstance(ctx, ChatContext):
        return ctx
    legacy = ctx or {}
    request_model = ChatRequest(message=msg)
    return ChatContext(
        request=request,
        request_model=request_model,
        message=msg,
        uid=str(legacy.get("uid") or ""),
        is_owner=bool(legacy.get("is_owner", False)),
        auth=getattr(getattr(request, "state", None), "auth", None),
    )


def _runtime_or_default(request: Any, runtime: ChatRuntime | None) -> ChatRuntime:
    if runtime is not None:
        return runtime
    # 兼容旧测试直接调用 handler(msg, request, ctx)；依赖仍从 API 兼容层
    # 实时组装，以便 monkeypatch app.api.chat.llm/knowledge 等继续生效。
    from app.api import chat as chat_api

    return chat_api._build_runtime(request)


def parse_time_question(msg: str) -> str | None:
    """「几点了/今天星期几/今天几号」按北京时间直答，不调用 LLM。"""
    if not TIME_QUESTION.search(msg):
        return None
    now = datetime.now(TZ)
    weekday = "一二三四五六日"[now.weekday()]
    hour = now.hour
    period = (
        "凌晨"
        if hour < 5
        else "早上"
        if hour < 9
        else "上午"
        if hour < 12
        else "中午"
        if hour < 13
        else "下午"
        if hour < 18
        else "晚上"
    )
    h12 = hour % 12 or 12
    return f"现在是{period} {h12}:{now.minute:02d} 啦（{now.month}月{now.day}日 星期{weekday}）"


def _enqueue_and_reply_impl(action: str, target: str, ctx: ChatContext, runtime: ChatRuntime) -> ChatResponse:
    services = runtime.services
    cmd_id = services.executor.enqueue(action, target)
    heartbeat = getattr(ctx.request.app.state, "collector_heartbeat", None)
    offline_note = ""
    if not computer_online(
        heartbeat,
        stale_seconds=runtime.settings.heartbeat_stale_seconds,
    ):
        offline_note = (
            "\n⚠️ 电脑当前不在线（采集器心跳超时）：指令已入队，"
            "开机后自动执行（30 分钟内有效）"
        )
    return ChatResponse(
        reply=(
            f"🤖 已收到指令（#{cmd_id}）：{action} → {target}\n"
            f"电脑上的执行器会处理，完成后我会在对话里告诉你结果{offline_note}"
        ),
        memories_used=0,
    )


async def _worklog(ctx: ChatContext, runtime: ChatRuntime) -> ChatResponse | None:
    if not (ctx.message.startswith("记录：") or ctx.message.startswith("记录:")):
        return None
    content = re.sub(r"^记录[:：]\s*", "", ctx.message)
    runtime.services.worklog.add_log(content)
    return ChatResponse(reply=f"已记录 ✓（{content}）", memories_used=0)


async def _time(ctx: ChatContext, runtime: ChatRuntime) -> ChatResponse | None:
    reply = parse_time_question(ctx.message)
    if reply is None:
        return None
    return ChatResponse(reply=reply, memories_used=0)


async def _reminders(ctx: ChatContext, runtime: ChatRuntime) -> ChatResponse | None:
    reminders = runtime.services.reminders
    msg = ctx.message
    reminder_cmd = reminders.parse_reminder_cmd(msg)
    if reminder_cmd:
        content, remind_at = reminder_cmd
        reminders.add_reminder(content, remind_at)
        return ChatResponse(
            reply=(
                f"⏰ 已设置提醒：{remind_at.strftime('%m月%d日 %H:%M')} → {content}\n"
                "到点后我会推 QQ 消息提醒你（手机必达）"
            ),
            memories_used=0,
        )
    if msg.strip() in ("我的提醒", "查看提醒", "有哪些提醒", "提醒列表"):
        pending = reminders.list_pending()
        if not pending:
            return ChatResponse(reply="目前没有待办提醒。", memories_used=0)
        lines = "\n".join(
            f"  {i + 1}. {item['content']}（{item['remind_at']}）"
            for i, item in enumerate(pending)
        )
        return ChatResponse(reply=f"⏰ 待办提醒：\n{lines}", memories_used=0)
    cancel_match = re.match(r"^(?:取消提醒|删除提醒)[：:\s]*(.+)$", msg)
    if cancel_match:
        count = reminders.cancel_by_keyword(cancel_match.group(1))
        return ChatResponse(
            reply=f"已取消 {count} 条相关提醒" if count else "没找到内容匹配的待办提醒",
            memories_used=0,
        )
    if msg.startswith("提醒"):
        return ChatResponse(
            reply=(
                "⏰ 设置提醒的句式：\n"
                "• 明早9点提醒我开会\n• 30分钟后提醒我喝水\n• 今晚8点提醒我看球\n"
                "• 我的提醒（查看）\n• 取消提醒：开会（取消）"
            ),
            memories_used=0,
        )
    return None


async def _documents(ctx: ChatContext, runtime: ChatRuntime) -> ChatResponse | None:
    documents = runtime.services.documents
    doc_cmd = documents.parse_doc_command(ctx.message)
    if not doc_cmd:
        return None
    title, requirement = doc_cmd
    result = await documents.generate_and_save(title, requirement)
    if "error" in result:
        return ChatResponse(reply=result["error"], memories_used=0)
    return ChatResponse(
        reply=(
            f"📄 文档已保存（#{result['id']}）：《{result['title']}》，"
            f"{result['words']} 字，已同步进知识库可检索"
        ),
        memories_used=0,
    )


async def _resume(ctx: ChatContext, runtime: ChatRuntime) -> ChatResponse | None:
    resume = runtime.services.resume
    target = resume.parse_resume_command(ctx.message)
    if target is None:
        return None
    result = await resume.optimize_resume(target_job=target)
    if "error" in result:
        return ChatResponse(reply=result["error"], memories_used=0)
    docx = result.get("docx", "")
    return ChatResponse(
        reply=(
            f"📄 简历优化完成（#{result['id']}）：《{result['title']}》\n"
            f"Word 文件：{docx}\n（用 scp 或 SFTP 从服务器取回；内容也已同步知识库可对话修改）"
        ),
        memories_used=0,
    )


async def _goals(ctx: ChatContext, runtime: ChatRuntime) -> ChatResponse | None:
    goals = runtime.services.goals
    goal_cmd = goals.parse_goal_command(ctx.message)
    if not goal_cmd:
        return None
    action, payload = goal_cmd
    if action == "create":
        goals.add_goal(payload, user_id=ctx.uid)
        return ChatResponse(reply=f"🎯 目标已记录：{payload}", memories_used=0)
    if action == "done":
        ok = goals.complete_goal(payload, user_id=ctx.uid)
        return ChatResponse(
            reply=f"🎉 目标已标记完成：{payload}" if ok else f"未找到匹配的活跃目标：{payload}",
            memories_used=0,
        )
    ok = goals.update_progress(payload, user_id=ctx.uid)
    return ChatResponse(
        reply=f"📈 进度已更新：{payload}" if ok else "暂无活跃目标可更新（先说\"目标：XXX\"创建）",
        memories_used=0,
    )


async def _fitness(ctx: ChatContext, runtime: ChatRuntime) -> ChatResponse | None:
    fitness = runtime.services.fitness
    msg = ctx.message
    weight = fitness.parse_weight(msg)
    if weight is not None:
        fitness.add_log("weight", weight, "")
        return ChatResponse(reply=f"⚖️ 体重已记录：{weight} kg ✓", memories_used=0)
    if msg.strip() in fitness.PROGRESS_WORDS:
        return ChatResponse(reply=fitness.fitness_summary(), memories_used=0)
    training = fitness.parse_training(msg)
    if training:
        fitness.add_log("training", None, training)
        return ChatResponse(reply=f"🏋️ 训练已记录 ✓（{training}）", memories_used=0)
    return None


async def _novel(ctx: ChatContext, runtime: ChatRuntime) -> ChatResponse | None:
    novel = getattr(runtime.services, "novel", None)
    novel_writing = novel.writer if novel is not None else runtime.services.novel_writing
    msg = ctx.message
    log_cmd = novel_writing.parse_writing_log(msg)
    if log_cmd:
        chapter, words = log_cmd
        novel_writing.add_writing_log(chapter, words)
        return ChatResponse(
            reply=f"📝 已记录写作：{f'第{chapter}章 ' if chapter else ''}{words} 字 ✓",
            memories_used=0,
        )
    if msg.strip() in ("写作进度", "写作统计", "写作台账", "写作记录查询"):
        return ChatResponse(reply=novel_writing.writing_summary(), memories_used=0)
    conflict_text = novel_writing.parse_conflict_command(msg)
    if conflict_text:
        if novel_writing.looks_like_file_path(conflict_text):
            return ChatResponse(
                reply=(
                    "📂 目前请直接粘贴正文来检查：把新写的内容贴在「检查设定冲突：」后面"
                    "（路径读取可先对文件说「读一下」拿到内容）"
                ),
                memories_used=0,
            )
        result = await (novel.review_conflicts(conflict_text) if novel is not None else novel_writing.check_conflicts(conflict_text))
        return ChatResponse(reply=result.reply if hasattr(result, "reply") else result["reply"], memories_used=0)
    # 小说写作二期：章节分析（1 次 LLM）+ 章节存档（零 LLM）。
    # 顺序在冲突检查之后（"检查设定冲突："不被吞）、续写之前。
    chapter_analysis = novel.chapters if novel is not None else runtime.services.chapter_analysis
    analysis_text = novel.parse_analysis_command(msg) if novel is not None else chapter_analysis.parse_analysis_command(msg)
    if analysis_text:
        if novel_writing.looks_like_file_path(analysis_text):
            return ChatResponse(
                reply=(
                    "📂 目前请直接粘贴正文来分析：把章节内容贴在「分析章节：」后面"
                    "（路径读取可先对文件说「读一下」拿到内容）"
                ),
                memories_used=0,
            )
        result = await (novel.review_chapter(analysis_text, user_id=ctx.uid) if novel is not None else chapter_analysis.analyze_chapter(analysis_text, user_id=ctx.uid))
        return ChatResponse(reply=result.reply if hasattr(result, "reply") else result["reply"], memories_used=0)
    archive = novel.parse_archive_command(msg) if novel is not None else chapter_analysis.parse_archive_command(msg)
    if archive:
        chapter, summary, threads = archive
        if novel is not None:
            novel.archive_chapter(chapter, summary, threads, source="manual")
        else:
            chapter_analysis.upsert_chapter_note(chapter, summary, threads, source="manual")
        note = f"，伏笔 {len(threads)} 条" if threads else ""
        return ChatResponse(
            reply=f"📖 第{chapter}章已存档{note}：{summary}",
            memories_used=0,
        )
    continue_text = novel_writing.parse_continue_command(msg)
    if continue_text:
        draft = await (novel.draft_chapter(continue_text) if novel is not None else novel_writing.continue_story(continue_text))
        reply = draft.text if hasattr(draft, "text") else draft
        return ChatResponse(reply=reply, memories_used=0)
    return None


async def _search(ctx: ChatContext, runtime: ChatRuntime) -> ChatResponse | None:
    message_search = runtime.services.message_search
    search_kw = message_search.parse_search_command(ctx.message)
    if search_kw is None:
        return None
    if not search_kw:
        return ChatResponse(
            reply="🔍 用法：搜索聊天记录：关键词\n多关键词用空格/逗号分隔（同时包含才算命中）",
            memories_used=0,
        )
    return ChatResponse(
        reply=message_search.format_results(search_kw, user_id=ctx.uid),
        memories_used=0,
    )


async def _identity(ctx: ChatContext, runtime: ChatRuntime) -> ChatResponse | None:
    if not ctx.is_owner:
        return None
    identity_guard = runtime.services.identity_guard
    confirm = runtime.services.confirm
    self_reflect = runtime.services.self_reflect
    uid = ctx.uid or ""

    if identity_guard.peek(uid) is not None:
        verdict = confirm.parse_reply(ctx.message)
        if verdict == "cancel":
            identity_guard.clear(uid)
            return ChatResponse(reply="好，那就不改，我还是小月。", memories_used=0)
        if verdict == "confirm":
            content = identity_guard.take(uid)
            if content is None:
                return ChatResponse(reply="刚那条改名已经过期了，需要的话再说一次。", memories_used=0)
            self_reflect.save_lesson(content, "")
            return ChatResponse(reply="好，记下了，以后就按这个来。", memories_used=0)
        identity_guard.clear(uid)

    verdict, reply = identity_guard.check(ctx.message)
    if verdict == "reject":
        return ChatResponse(reply=reply, memories_used=0)
    if verdict == "confirm":
        identity_guard.remember(uid, ctx.message)
        return ChatResponse(reply=reply, memories_used=0)
    return None


async def _confirm(ctx: ChatContext, runtime: ChatRuntime) -> ChatResponse | None:
    confirm = runtime.services.confirm
    if confirm.peek(ctx.uid or "") is None:
        return None
    verdict = confirm.parse_reply(ctx.message)
    if verdict is None:
        confirm.clear(ctx.uid or "")
        return None
    if verdict == "cancel":
        confirm.clear(ctx.uid or "")
        return ChatResponse(reply="好，已取消。", memories_used=0)
    item = confirm.take(ctx.uid or "")
    if item is None:
        return ChatResponse(reply="刚才那条指令已经超时失效了，需要的话再说一次。", memories_used=0)
    return _enqueue_and_reply_impl(item["action"], item["target"], ctx, runtime)


async def _slang(ctx: ChatContext, runtime: ChatRuntime) -> ChatResponse | None:
    slang = runtime.services.slang
    msg = ctx.message
    correction = slang.parse_correct(msg)
    if correction:
        term, meaning = correction
        if slang._own_term(term, user_id=ctx.uid):
            slang.save_term(term, meaning, user_id=ctx.uid, status="confirmed")
            return ChatResponse(reply=f"🤝 已更新黑话「{term}」的意思", memories_used=0)
        return None
    teaching = slang.parse_teach(msg)
    if teaching:
        term, meaning = teaching
        scope = "shared" if ctx.is_owner else "private"
        slang.save_term(term, meaning, user_id=ctx.uid, scope=scope)
        note = "（已共享给访客）" if scope == "shared" else "（仅你自己可见）"
        return ChatResponse(reply=f"📔 记下黑话：「{term}」＝{meaning}{note}", memories_used=0)
    if msg.strip() in ("黑话列表", "我的黑话"):
        rows = slang.list_terms(user_id=ctx.uid)
        if not rows:
            return ChatResponse(reply="还没有黑话条目。说「记黑话：词 = 意思」来教小月。", memories_used=0)
        lines = "\n".join(
            f"  「{row['term']}」＝{row['meaning'][:40]}"
            + ("（共享）" if row["scope"] == "shared" else "（私有）")
            + ("" if row["status"] == "confirmed" else "（候选）")
            for row in rows[:10]
        )
        return ChatResponse(reply=f"📔 黑话列表：\n{lines}", memories_used=0)
    if ctx.is_owner:
        match = re.match(r"^(?:黑话共享|黑话私藏|黑话删除)[：:]\s*(.+)$", msg.strip())
        if match:
            term = match.group(1).strip()[:12]
            if msg.strip().startswith("黑话共享"):
                count = slang.set_scope(term, "shared", user_id=ctx.uid)
                return ChatResponse(
                    reply=f"🌐 「{term}」已设为共享" if count else f"没找到你的黑话「{term}」",
                    memories_used=0,
                )
            if msg.strip().startswith("黑话私藏"):
                count = slang.set_scope(term, "private", user_id=ctx.uid)
                return ChatResponse(
                    reply=f"🔒 「{term}」已私藏" if count else f"没找到你的黑话「{term}」",
                    memories_used=0,
                )
            count = slang.delete_term(term, user_id=ctx.uid)
            return ChatResponse(
                reply=f"🗑 已删除「{term}」" if count else f"没找到你的黑话「{term}」",
                memories_used=0,
            )
    return None


async def _entity_candidates(ctx: ChatContext, runtime: ChatRuntime) -> ChatResponse | None:
    index_healer = runtime.services.index_healer
    msg = ctx.message
    if msg.strip() in ("查看候选抽取", "候选抽取", "候选实体"):
        rows = index_healer.candidate_list()
        if not rows:
            return ChatResponse(reply="🧪 候选池是空的（自动抽取尚未产生低置信候选）。", memories_used=0)
        lines = "\n".join(
            f"  {row['name']}（{row['kind']}·{(row['book'] or '').replace('小说-', '')}）"
            for row in rows[:10]
        )
        return ChatResponse(
            reply=(
                f"🧪 待确认的抽取候选：\n{lines}\n"
                "转正用「确认抽取：名字」，不要的用「废弃抽取：名字」"
            ),
            memories_used=0,
        )
    match = re.match(r"^(?:确认抽取|废弃抽取)[：:]\s*(.+)$", msg.strip())
    if match:
        name = match.group(1).strip()[:20]
        if msg.strip().startswith("确认"):
            count = index_healer.candidate_confirm(name)
            return ChatResponse(
                reply=f"✅ 已把「{name}」转正进实体索引" if count else f"候选池里没有「{name}」",
                memories_used=0,
            )
        count = index_healer.candidate_discard(name)
        return ChatResponse(
            reply=f"🗑 已废弃「{name}」" if count else f"候选池里没有「{name}」",
            memories_used=0,
        )
    return None


async def _executor(ctx: ChatContext, runtime: ChatRuntime) -> ChatResponse | None:
    executor = runtime.services.executor
    confirm = runtime.services.confirm
    msg = ctx.message
    exec_cmd = executor.parse_executor_command(msg)
    if not exec_cmd:
        return None
    action, target = exec_cmd
    paths = executor.unpack_paths(action, target)
    if action == "open":
        if not executor.check_open_target(target):
            return ChatResponse(reply="🔒 打开目标不在白名单或不是已登记别名，已拒绝", memories_used=0)
        if not executor.plausible_open_target(target):
            return None
    elif not (action == "search_files" and not paths):
        if not paths or not all(executor.check_roots(path) for path in paths):
            return ChatResponse(
                reply="🔒 该操作超出白名单目录（EXECUTOR_ALLOWED_ROOTS），已拒绝",
                memories_used=0,
            )

    confident = action != "open" or executor.confident_open_target(target)
    if confirm.needs_confirm(action, target, confident=confident):
        desc = executor.describe_command(action, target)
        confirm.remember(ctx.uid or "", action, target, desc)
        return ChatResponse(
            reply=f"❓ 需要我{desc}吗？\n回复「确认」执行，「取消」放弃（3 分钟内有效）",
            memories_used=0,
        )
    return _enqueue_and_reply_impl(action, target, ctx, runtime)


# 兼容旧调用签名：canonical 形态是 (ChatContext, ChatRuntime)，旧测试仍可传
# (msg, request, ctx)。
async def _invoke_handler(
    handler: Any,
    first: Any,
    second: Any,
    legacy_ctx: dict | ChatContext | None = None,
    runtime: ChatRuntime | None = None,
) -> ChatResponse | None:
    if isinstance(first, ChatContext) and isinstance(second, ChatRuntime):
        return await handler(first, second)
    return await handler(
        _coerce_context(str(first), second, legacy_ctx),
        _runtime_or_default(second, runtime),
    )


async def _handle_worklog(first: Any, second: Any, ctx: dict | ChatContext | None = None, runtime: ChatRuntime | None = None) -> ChatResponse | None:
    return await _invoke_handler(_worklog, first, second, ctx, runtime)


async def _handle_time(first: Any, second: Any, ctx: dict | ChatContext | None = None, runtime: ChatRuntime | None = None) -> ChatResponse | None:
    return await _invoke_handler(_time, first, second, ctx, runtime)


async def _handle_reminders(first: Any, second: Any, ctx: dict | ChatContext | None = None, runtime: ChatRuntime | None = None) -> ChatResponse | None:
    return await _invoke_handler(_reminders, first, second, ctx, runtime)


async def _handle_documents(first: Any, second: Any, ctx: dict | ChatContext | None = None, runtime: ChatRuntime | None = None) -> ChatResponse | None:
    return await _invoke_handler(_documents, first, second, ctx, runtime)


async def _handle_resume(first: Any, second: Any, ctx: dict | ChatContext | None = None, runtime: ChatRuntime | None = None) -> ChatResponse | None:
    return await _invoke_handler(_resume, first, second, ctx, runtime)


async def _handle_goals(first: Any, second: Any, ctx: dict | ChatContext | None = None, runtime: ChatRuntime | None = None) -> ChatResponse | None:
    return await _invoke_handler(_goals, first, second, ctx, runtime)


async def _handle_fitness(first: Any, second: Any, ctx: dict | ChatContext | None = None, runtime: ChatRuntime | None = None) -> ChatResponse | None:
    return await _invoke_handler(_fitness, first, second, ctx, runtime)


async def _handle_novel(first: Any, second: Any, ctx: dict | ChatContext | None = None, runtime: ChatRuntime | None = None) -> ChatResponse | None:
    return await _invoke_handler(_novel, first, second, ctx, runtime)


async def _handle_search(first: Any, second: Any, ctx: dict | ChatContext | None = None, runtime: ChatRuntime | None = None) -> ChatResponse | None:
    return await _invoke_handler(_search, first, second, ctx, runtime)


async def _handle_identity(first: Any, second: Any, ctx: dict | ChatContext | None = None, runtime: ChatRuntime | None = None) -> ChatResponse | None:
    return await _invoke_handler(_identity, first, second, ctx, runtime)


async def _handle_confirm(first: Any, second: Any, ctx: dict | ChatContext | None = None, runtime: ChatRuntime | None = None) -> ChatResponse | None:
    return await _invoke_handler(_confirm, first, second, ctx, runtime)


async def _handle_slang(first: Any, second: Any, ctx: dict | ChatContext | None = None, runtime: ChatRuntime | None = None) -> ChatResponse | None:
    return await _invoke_handler(_slang, first, second, ctx, runtime)


async def _handle_entity_candidates(first: Any, second: Any, ctx: dict | ChatContext | None = None, runtime: ChatRuntime | None = None) -> ChatResponse | None:
    return await _invoke_handler(_entity_candidates, first, second, ctx, runtime)


async def _handle_executor(first: Any, second: Any, ctx: dict | ChatContext | None = None, runtime: ChatRuntime | None = None) -> ChatResponse | None:
    return await _invoke_handler(_executor, first, second, ctx, runtime)


def _enqueue_and_reply(action: str, target: str, request: Any, runtime: ChatRuntime | None = None) -> ChatResponse:
    context = _coerce_context("", request, {"uid": "", "is_owner": True})
    return _enqueue_and_reply_impl(action, target, context, _runtime_or_default(request, runtime))


_COMMAND_HANDLERS: list[tuple[str, object]] = [
    ("identity", _handle_identity),
    ("confirm", _handle_confirm),
    ("worklog", _handle_worklog),
    ("time", _handle_time),
    ("reminders", _handle_reminders),
    ("documents", _handle_documents),
    ("resume", _handle_resume),
    ("goals", _handle_goals),
    ("fitness", _handle_fitness),
    ("novel", _handle_novel),
    ("search", _handle_search),
    ("slang", _handle_slang),
    ("entity_candidates", _handle_entity_candidates),
    ("executor", _handle_executor),
]


async def dispatch(ctx: ChatContext, runtime: ChatRuntime) -> ChatResponse | None:
    """按注册顺序执行命令，访客跳过主人专属 handler。"""
    for name, handler in _COMMAND_HANDLERS:
        if not ctx.is_owner and name in GUEST_BLOCKED_HANDLERS:
            continue
        response = await handler(
            ctx.message,
            ctx.request,
            ctx.as_legacy_dict(),
            runtime=runtime,
        )
        if response is not None:
            return response
    return None
