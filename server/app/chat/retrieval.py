"""聊天检索层。

本模块负责一轮消息的历史读取、记忆/知识库/实体检索、自愈诊断，以及为 prompt
准备的动态数据。所有查询都显式携带 uid；访客路径不会读取主人知识库和专属注入。
它不组装最终消息、不调用主聊天 LLM。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import re
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAIError

from app.chat.context import ChatContext, ChatRuntime
from app.core import embedding
from app.models.database import connect
from app.services import sepia

logger = logging.getLogger("assistant.chat.retrieval")


@dataclass
class TurnPreparation:
    last_ai: str | None = None
    definition_term: str | None = None


def _detect_domains(detector, query: str, user_id: str):
    """调用域判定器；兼容旧的单参数测试替身/外部插件。"""
    try:
        parameters = inspect.signature(detector).parameters.values()
    except (TypeError, ValueError):
        return detector(query, user_id=user_id)
    supports_user_id = any(
        parameter.name == "user_id" or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )
    return detector(query, user_id=user_id) if supports_user_id else detector(query)


def _call_with_user(func, *args, user_id: str):
    """调用支持主体参数的注入器；兼容旧插件/测试替身。"""
    return _call_with_context(func, *args, user_id=user_id)


def _call_with_context(
    func,
    *args,
    user_id: str | None = None,
    request_id: str | None = None,
):
    """按函数签名传递主体/请求上下文，兼容旧插件/测试替身。"""
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        kwargs = {"user_id": user_id, "request_id": request_id}
        return func(*args, **{k: v for k, v in kwargs.items() if v is not None})
    kwargs = {}
    if user_id is not None and (
        "user_id" in parameters or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()
        )
    ):
        kwargs["user_id"] = user_id
    if request_id is not None and (
        "request_id" in parameters or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()
        )
    ):
        kwargs["request_id"] = request_id
    return func(*args, **kwargs)


_FOLLOWUP_RE = re.compile(
    r"(?:他|她|它|这|那|这个|那个|其|该|上述|前面|上次|刚才|后来|然后|接着|继续|再|还|以及|详细讲讲|展开说说|具体说说|再确认|什么意思|怎么回事|为什么呢|然后呢|后来呢)"
)
_CONFIRM_RE = re.compile(r"^\s*(?:谢谢|好的|好呀|好哦|嗯+|哦+|行|收到|明白|知道了|ok|OK|行了)[!！。\.、,， ]*$")
_NEW_DOMAIN_RE = re.compile(
    r"(?:简历|求职|岗位|面试|服务器|部署|运维|数据库|接口|教程|怎么装|如何配置|项目|代码|程序|报错|测试|健身|训练|饮食)"
)
_ANCHOR_STOPWORDS = {
    "怎么", "如何", "什么", "为什么", "哪个", "哪些", "一下", "一下子", "然后", "后来",
    "详细", "具体", "讲讲", "说说", "继续", "接着", "谢谢", "好的", "那个", "这个", "上次",
}
_MAX_ANCHORS = 8
IMAGE_SEARCH_PLACEHOLDER = "图片"
_ANCHOR_DOMAIN_TERMS = (
    "简历", "求职", "岗位", "面试", "服务器", "部署", "运维", "数据库", "接口",
    "教程", "反代", "知识库", "检索", "向量", "embedding", "prompt", "项目", "健身", "训练", "饮食",
)


def _history_texts(history: Iterable[dict[str, Any]]) -> list[str]:
    return [str(item.get("content") or "").strip() for item in history if item.get("content")]


def _is_followup(message: str) -> bool:
    msg = (message or "").strip()
    if not msg or _CONFIRM_RE.fullmatch(msg) or _NEW_DOMAIN_RE.search(msg):
        return False
    return bool(_FOLLOWUP_RE.search(msg))


def _extract_anchor_candidates(
    message: str,
    history: list[dict[str, Any]],
    known_anchors: Iterable[str] = (),
) -> list[str]:
    """从索引词表和近轮历史抽取少量稳定锚点，避免把整句塞进 BM25。"""
    texts = _history_texts(history)
    joined = "\n".join(texts)
    known = {
        str(word).strip() for word in known_anchors
        if str(word).strip() and len(str(word).strip()) >= 2
    }
    known.update(term for term in _ANCHOR_DOMAIN_TERMS if term in joined)
    found: list[tuple[int, int, str]] = []
    for word in known:
        if word in joined or word in (message or ""):
            # 当前句命中的锚点优先，其次按历史最近出现位置排序。
            pos = max((joined.rfind(word), (message or "").find(word)))
            found.append((1 if word in (message or "") else 0, pos, word))

    # 显式引号/书名号内容通常是用户自己的项目、术语或黑话。
    for text in texts[-3:]:
        for match in re.findall(r"[《「『“‘]([^》」』”’]{2,30})[》」』”’]", text):
            if match not in _ANCHOR_STOPWORDS:
                found.append((2, joined.rfind(match), match))

    # 只保留历史中重复出现的中文/英文术语，降低整句和功能词污染。
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[一-龥]{2,10}", joined)
    counts: dict[str, int] = {}
    for token in tokens:
        if token not in _ANCHOR_STOPWORDS and len(token) >= 2:
            counts[token] = counts.get(token, 0) + 1
    for token, count in counts.items():
        if count >= 2 or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{2,}", token):
            found.append((0, joined.rfind(token), token))

    selected: list[str] = []
    seen: set[str] = set()
    for _, _, word in sorted(found, key=lambda item: (-item[0], -item[1], -len(item[2]))):
        if word in seen or word in _ANCHOR_STOPWORDS:
            continue
        if any(word in prior or prior in word for prior in selected):
            continue
        selected.append(word)
        seen.add(word)
        if len(selected) >= _MAX_ANCHORS:
            break
    return selected


def build_search_query(
    message: str,
    history: list[dict[str, Any]],
    *,
    known_anchors: Iterable[str] = (),
) -> tuple[str, list[str], bool]:
    """仅为检索构造 query；返回 ``(query, anchors, expanded)``。"""
    original = message or ""
    if not _is_followup(original):
        return original, [], False
    anchors = _extract_anchor_candidates(original, history, known_anchors)
    if anchors:
        return f"{original} {' '.join(anchors)}", anchors, True
    substantive = next(
        (item["content"] for item in reversed(history)
         if item.get("role") == "user" and len((item.get("content") or "").strip()) >= 4),
        "",
    )
    fallback = substantive.strip()[:80]
    if fallback and fallback != original:
        return f"{original} {fallback}", [], True
    return original, [], False


@dataclass
class RetrievalBundle:
    mems: list[dict[str, Any]] = field(default_factory=list)
    injections: str = ""
    knowledge_text: str = ""
    healed_text: str = ""
    entity_ctx: str = ""
    intent_label: str = ""
    trace: dict[str, Any] = field(
        default_factory=lambda: {
            "routing": {},
            "path": "hybrid",
            "degraded": 0,
            "healer_words": [],
            "search_ms": 0,
            "original_query": "",
            "search_query": "",
            "anchors": [],
            "expanded": False,
            "retrieval": {
                "memory_candidates": 0,
                "memory_selected": 0,
                "knowledge_candidates": 0,
                "knowledge_selected": 0,
                "entity_hits": 0,
                "healed_chunks": 0,
            },
        }
    )
    last_ai: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    older: list[str] = field(default_factory=list)
    definition_term: str | None = None
    profile: str = ""
    lessons: str = ""
    concerns: str = ""
    jargon: str = ""
    style_examples: str = ""
    facts: str = ""
    behavior: str = ""
    goals_text: str = ""
    open_issues: str = ""
    slang: str = ""
    mood: str = ""
    mood_state: str = ""
    self_state: str = ""
    extra_blocks: list[str] = field(default_factory=list)


def track_background(runtime: ChatRuntime, awaitable: Any) -> asyncio.Task:
    """创建并保留 fire-and-forget 任务引用，避免任务被 GC 回收。"""
    task = asyncio.create_task(awaitable)
    runtime.bg_tasks.add(task)
    task.add_done_callback(runtime.bg_tasks.discard)
    return task


def _last_assistant_message(runtime: ChatRuntime, uid: str) -> str | None:
    memory = runtime.memory
    conn = connect()
    try:
        scope_clause, scope_args = memory._user_scope(uid)
        row = conn.execute(
            f"SELECT content FROM memories WHERE sender='assistant' AND {scope_clause} "
            "ORDER BY id DESC LIMIT 1",
            scope_args,
        ).fetchone()
        return row["content"] if row else None
    finally:
        conn.close()


def prepare_turn(ctx: ChatContext, runtime: ChatRuntime) -> TurnPreparation:
    """执行检索前的纠正/风格/事实桥接，并取得上一条 AI 回复。"""
    services = runtime.services
    last_ai = _last_assistant_message(runtime, ctx.uid)
    self_reflect = services.self_reflect
    identity_guard = services.identity_guard

    # 教训入库：身份类可在第一轮直接落库，普通纠正仍要求存在上一条 AI 回复。
    if (
        ctx.is_owner
        and self_reflect.detect_correction(ctx.message)
        and not identity_guard.is_roleplay_or_insult(ctx.message)
    ):
        if self_reflect.classify_lesson(ctx.message) == "identity":
            self_reflect.save_lesson(ctx.message, last_ai or "", user_id=ctx.uid)
        elif last_ai:
            self_reflect.save_lesson(ctx.message, last_ai, user_id=ctx.uid)

    if services.few_shot.detect_positive_feedback(ctx.message) and last_ai:
        services.few_shot.save_example(last_ai, user_id=ctx.uid)

    fact_extract = services.fact_extract
    if ctx.is_owner and last_ai and (
        fact_extract.is_record_command(ctx.message)
        or (
            fact_extract.is_short_confirm(ctx.message)
            and fact_extract.last_ai_looks_like_setting(last_ai)
        )
    ):
        track_background(
            runtime,
            fact_extract.extract_from_last_ai(last_ai, user_id=ctx.uid),
        )

    return TurnPreparation(
        last_ai=last_ai,
        definition_term=services.jargon.detect_definition(ctx.message),
    )


def _known_index_anchors(ctx: ChatContext) -> set[str]:
    """主人侧读取现有索引词表；访客绝不触达主人专属实体/知识表。"""
    if not ctx.is_owner:
        return set()
    try:
        from app.services import novel_lexicon

        return novel_lexicon.known_index_anchors()
    except (ImportError, AttributeError, KeyError, TypeError, ValueError) as exc:
        # 词表不可用时仍允许历史中的显式术语安全降级。
        logger.debug("小说锚点词表不可用: %s", exc)
        return set()


def _collect_injections(ctx: ChatContext, runtime: ChatRuntime, msg: str) -> dict[str, Any]:
    """收集全部 prompt 注入段（同步 DB ×~20，整体供 to_thread 调用）。

    注入器均为纯查询/写入，无后台任务调度——prepare_turn 那种
    "工作线程里没有事件循环"的限制在这里不存在。
    执行顺序与拆分前逐项一致，保持注入语义不变。
    """
    settings = runtime.settings
    services = runtime.services
    memory = runtime.memory
    owner = ctx.is_owner

    profile = services.profile.get_profile_injection(user_id=ctx.uid)
    lessons = services.self_reflect.get_lessons_injection(user_id=ctx.uid) if owner else ""
    concerns = services.concern_tracker.get_concerns_injection(user_id=ctx.uid)
    jargon = services.jargon.get_jargon_injection(msg, user_id=ctx.uid)
    style_examples = services.few_shot.get_examples_injection(user_id=ctx.uid)
    facts = memory.get_facts_injection(user_id=ctx.uid)
    behavior = (
        services.behavior_context.get_behavior_injection(user_id=ctx.uid)
        if owner and settings.behavior_inject_enabled
        else ""
    )
    goals_text = services.goals.get_goals_injection(user_id=ctx.uid)
    open_issues = services.unresolved.get_open_issues_injection(user_id=ctx.uid)
    slang = services.slang.get_slang_injection(msg, user_id=ctx.uid)
    mood_text = services.mood.detect_mood(msg) if owner else ""
    mood_state = services.mood.get_state_injection(user_id=ctx.uid) if owner else ""
    self_state = services.self_state.get_self_state_injection(user_id=ctx.uid)

    extra_blocks: list[str] = []
    if owner and services.growth.detect_self_doubt(msg):
        block = services.growth.build_injection(user_id=ctx.uid)
        if block:
            extra_blocks.append(block)
    if owner:
        services.intent_goals.record_intent(msg, user_id=ctx.uid)
        followup = services.intent_goals.build_injection(user_id=ctx.uid)
        if followup:
            extra_blocks.append(followup)
        hint = services.knowledge_hint.build_hint(msg)
        if hint:
            extra_blocks.append(hint)

    older = memory.get_older_summaries(window_size=settings.history_limit, user_id=ctx.uid)

    # 小说写作二期：写第 N 章/续写时注入前情提要 + 未回收伏笔（表空则不出现）。
    # 续写短命令「继续/接着写」不含章节字样但会进生成档，所以用 gen 判据全集。
    from app.chat.prompting import _GENERATION_INTENT

    if ctx.image is None and owner and _GENERATION_INTENT.search(msg):
        generation_block = sepia.build_generation_block()
        if generation_block:
            extra_blocks.append(generation_block)
        try:
            block = services.chapter_analysis.build_continuity_block()
        except (sqlite3.Error, KeyError, TypeError, ValueError, AttributeError) as exc:
            runtime.logger.warning("[chapter] 前情提要构建失败（不影响主回复）: %s", exc)
            block = ""
        if block:
            extra_blocks.append(block)

    return {
        "profile": profile,
        "lessons": lessons,
        "concerns": concerns,
        "jargon": jargon,
        "style_examples": style_examples,
        "facts": facts,
        "behavior": behavior,
        "goals_text": goals_text,
        "open_issues": open_issues,
        "slang": slang,
        "mood": mood_text,
        "mood_state": mood_state,
        "self_state": self_state,
        "older": older,
        "extra_blocks": extra_blocks,
    }


async def retrieve(ctx: ChatContext, runtime: ChatRuntime, preparation: TurnPreparation) -> RetrievalBundle:
    """完成记忆、知识库、自愈与 prompt 动态数据收集。

    记忆与知识两路检索并发执行（只共享 search_query，无数据依赖）；
    外层开 embedding 请求级去重作用域，同一 query 的多次 embed 只打一次 API。
    """
    settings = runtime.settings
    memory = runtime.memory
    knowledge = runtime.knowledge
    services = runtime.services
    msg = ctx.message
    history = memory.get_recent_history(settings.history_limit, user_id=ctx.uid)
    known_anchors = _known_index_anchors(ctx)
    # 无 caption 的图片也必须走稳定的非空检索 query，避免 embedding/FTS 空串异常。
    query_text = msg if msg or ctx.image is None else IMAGE_SEARCH_PLACEHOLDER
    search_query, anchors, expanded = build_search_query(
        query_text, history, known_anchors=known_anchors
    )

    with embedding.query_scope():
        mem_task = asyncio.ensure_future(
            memory.search(
                search_query,
                top_k=settings.inject_top_k,
                min_similarity=settings.min_similarity,
                user_id=ctx.uid,
            )
        )
        know_task = (
            asyncio.ensure_future(knowledge.search_knowledge(search_query, top_k=4))
            if ctx.is_owner
            else None
        )
        started = time.monotonic()

        healed_text = ""
        knowledge_text = ""
        entity_ctx = ""
        trace: dict[str, Any] = {
            "routing": {},
            "path": "hybrid",
            "degraded": 0,
            "healer_words": [],
            "search_ms": 0,
            "original_query": msg,
            "search_query": search_query,
            "anchors": anchors,
            "expanded": expanded,
            "retrieval": {
                "memory_candidates": 0,
                "memory_selected": 0,
                "knowledge_candidates": 0,
                "knowledge_selected": 0,
                "entity_hits": 0,
                "healed_chunks": 0,
            },
        }

        # 域判定只服务 trace 与 healer（search_knowledge 内部自行判域），
        # 与两路检索无数据依赖，放在任务之后并发消化。
        if ctx.is_owner:
            detect_domains = services.knowledge_domain.detect_domains
            domains, docs = _detect_domains(detect_domains, search_query, ctx.uid)
            trace["routing"] = {"domains": domains, "docs": docs}
            if "__skip__" in domains:
                trace["path"] = "skip"

        mems = await mem_task
        trace["retrieval"]["memory_candidates"] = len(mems)
        if not mems or mems[0].get("score", 0) < 0.12:
            deep = memory.deep_keyword_search(search_query, top_k=5, user_id=ctx.uid)
            if deep:
                known = {item["id"] for item in mems}
                mems = deep + [item for item in mems if item["id"] not in known]
        mems = services.cooccurrence.expand(mems, user_id=ctx.uid)
        trace["retrieval"]["memory_selected"] = len(mems)
        injections = _call_with_user(
            services.subjective_time.format_injection,
            mems,
            user_id=ctx.uid,
        )

        knowledge_hits: list[dict] = []
        if know_task is not None:
            knowledge_hits = await know_task
            trace["search_ms"] = int((time.monotonic() - started) * 1000)
            trace["degraded"] = 1 if knowledge.last_vector_degraded() else 0
            trace["retrieval"]["knowledge_candidates"] = len(knowledge_hits)
            knowledge_hits = knowledge.expand_chunks(knowledge_hits, radius=1, max_chars=1500)
            trace["retrieval"]["knowledge_selected"] = len(knowledge_hits)

            index_healer = services.index_healer
            if settings.healer_enabled:
                try:
                    diagnosis = index_healer.diagnose(search_query, domains, docs, knowledge_hits)
                    if diagnosis is not None:
                        healed_text, healed_chunks = await _call_with_context(
                            index_healer.heal,
                            diagnosis,
                            search_query,
                            user_id=ctx.uid,
                            request_id=ctx.request_id,
                        )
                        if healed_text:
                            trace["_heal_words"] = list(diagnosis["words"])
                            trace["retrieval"]["healed_chunks"] = len(healed_chunks)
                            runtime.logger.info(
                                "[healer] 兜底提炼生效: %s → %d 块",
                                diagnosis["words"],
                                len(healed_chunks),
                            )
                            domain = index_healer.classify_aggregate_domain(healed_chunks)
                            register_class = services.knowledge_domain.register_class
                            for word in diagnosis["words"]:
                                register_class(word, domain=domain, source_query=search_query[:200])
                            auto_book = index_healer.majority_novel_book(healed_chunks)
                            if auto_book:
                                track_background(
                                    runtime,
                                    _call_with_context(
                                        index_healer.auto_extract_task,
                                        diagnosis["words"],
                                        auto_book,
                                        user_id=ctx.uid,
                                        request_id=ctx.request_id,
                                    ),
                                )
                except (OpenAIError, TimeoutError, RuntimeError, sqlite3.Error, KeyError, TypeError, ValueError) as exc:
                    runtime.logger.warning("[healer] 自愈流程异常（不影响主回复）: %s", exc)

            knowledge_text = knowledge.format_knowledge_injection(knowledge_hits)
            alias_note = knowledge.get_alias_note(search_query)
            if alias_note:
                knowledge_text = f"（背景：{alias_note}）\n" + knowledge_text

            entity_ctx = services.novel_entities.build_entity_context(search_query)
            if entity_ctx:
                trace["retrieval"]["entity_hits"] = 1
                knowledge_text = entity_ctx + "\n\n" + knowledge_text
                trace["path"] = "entity"

            novel_facts = knowledge.get_novel_facts(search_query)
            if novel_facts:
                knowledge_text = (
                    "【小说设定卡（知识库权威资料，回答时直接采用）】\n- "
                    + "\n- ".join(novel_facts)
                    + "\n\n"
                    + knowledge_text
                )

            fitness_cards = services.fitness.get_fitness_facts(search_query)
            if fitness_cards:
                knowledge_text = (
                    "【健身知识卡（权威资料，可注明出处年份）】\n"
                    "用户若提出训练/饮食安排，先据此逐项核对：动作选择是否重复、"
                    "容量与次数是否匹配动作类型、相邻训练日是否有肌群恢复冲突。"
                    "发现问题直接说明并给替代方案；没问题才确认。不要只复述用户的计划。\n- "
                    + "\n- ".join(fitness_cards)
                    + "\n\n"
                    + knowledge_text
                )

        # 实时检索通道：仅在响应计划要求联网时触发。
        # 无来源时不注入任何内容，并在计划里打标记，由提示词强制"未查到"口径。
        plan = dict(getattr(ctx.trace, "response_plan", {}) or {})
        if ctx.is_owner and plan.get("provider") == "hotboard":
            # 热点浏览：拉热榜（直连），注入为"当下热议话题清单"
            from app.chat import hotboard_provider

            if hotboard_provider.configured():
                items = await hotboard_provider.fetch_hotboard()
                plan["hotboard_count"] = len(items)
                if items:
                    board = hotboard_provider.format_hotboard(items)
                    knowledge_text = board + "\n\n" + knowledge_text
                    plan["hotboard_ok"] = True
                else:
                    plan["hotboard_empty"] = True
            else:
                plan["hotboard_unavailable"] = True
            ctx.trace.response_plan = plan
        elif ctx.is_owner and plan.get("provider") == "web_search":
            from app.chat import web_provider

            if not web_provider.configured():
                plan["web_unavailable"] = True
            else:
                # 主检索词用用户原话（provider 会剥掉口语包装），planner 的
                # 改写只作备用：改写可能收窄成生僻词，原话反而更全。
                query = str(msg or plan.get("query") or "").strip()
                alt = str(plan.get("query") or "").strip()
                plan["web_query"] = query[:200]
                # 深挖：事件核查/道德判断类问题多搜几组正交角度，主动找可能
                # 推翻当前印象的信息，减少"靠用户追问才补上关键事实"的情况。
                deep = plan.get("intent") in {"event_lookup", "moral_judgment"} or bool(
                    plan.get("needs_moral_judgment")
                )
                data = await web_provider.search_and_cluster(
                    query,
                    time_range=web_provider.time_range_default(),
                    alt_query=alt if alt and alt != query else None,
                    deep_dive=deep,
                )
                if deep:
                    plan["web_deep_dive"] = True
                    plan["web_angle_added"] = int(data.get("angle_added") or 0)
                if data["has_sources"]:
                    plan["web_has_sources"] = True
                    plan["web_fallback"] = bool(data.get("fallback_used"))
                    plan["web_query_used"] = str(data.get("query_used") or "")[:200]
                    plan["web_attempts"] = int(data.get("attempts") or 0)
                    plan["web_report_count"] = len(data["results"])
                    plan["web_event_count"] = len(data["events"])
                    plan["web_observed_at"] = data["observed_at"]
                    plan["web_gdelt_count"] = int(data.get("gdelt_count") or 0)
                    from app.chat import source_analysis

                    analysis = source_analysis.analyze_sources(data["results"])
                    plan["web_origin_count"] = analysis.confirmed_count
                    plan["web_max_reprint"] = analysis.max_reprint
                    plan["web_unknown_count"] = analysis.unknown_count
                    sources_text = web_provider.format_sources(data["results"])
                    events_text = web_provider.format_events(data["events"])

                    # 声明级比对：仅在报道数达标时做，单一信源无可比对。
                    claim_text = ""
                    if getattr(runtime.settings, "claim_analysis_enabled", True) and (
                        len(data["results"])
                        >= int(getattr(runtime.settings, "claim_analysis_min_reports", 2))
                    ):
                        from app.chat import claim_analysis

                        rule_cs = claim_analysis.extract_claims_rule(data["results"])
                        llm_cs = await claim_analysis.extract_claims_llm(data["results"], runtime)
                        report = claim_analysis.analyze_claims(rule_cs, llm_cs or None)
                        claim_text = report.text
                        plan["web_claim_conflicts"] = report.conflict_count
                        plan["web_claim_singles"] = report.single_count
                        plan["web_claim_extracted"] = report.extracted_by_llm

                    block = "【实时检索资料（本轮新获取）】\n"
                    # 先给独立性核查：它决定"多条报道"能不能当作多方印证
                    analysis_text = analysis.render()
                    if analysis_text:
                        block += analysis_text + "\n\n"
                    # 再给声明级比对：具体哪个事实点对不上、是谁说的
                    if claim_text:
                        block += "【多来源事实比对】\n" + claim_text + "\n\n"
                    if events_text:
                        block += events_text + "\n\n"
                    block += sources_text
                    knowledge_text = block + "\n\n" + knowledge_text
                else:
                    plan["web_no_sources"] = True
            ctx.trace.response_plan = plan

        from app.chat.prompting import _untrusted_reference

        if knowledge_text:
            knowledge_text = _untrusted_reference("知识库、实体与资料卡", knowledge_text)
        if healed_text:
            healed_text = _untrusted_reference("检索自愈聚合", healed_text)

        index_healer = services.index_healer
        intent_label = ""
        if ctx.is_owner:
            if healed_text:
                intent_label = "healed"
                trace["path"] = "heal"
                trace["healer_words"] = list(trace.get("_heal_words", []))
            elif entity_ctx:
                intent_label = "entity"
            elif trace["routing"].get("domains") and index_healer.detect_enum_intent(search_query):
                intent_label = "enum"
            elif trace["routing"].get("docs") or trace["routing"].get("domains") == ["novel"]:
                intent_label = "novel"

        # 注入收集整体（同步 SQLite ×~20）迁工作线程
        collected = await asyncio.to_thread(_collect_injections, ctx, runtime, msg)

    return RetrievalBundle(
        mems=mems,
        injections=injections,
        knowledge_text=knowledge_text,
        healed_text=healed_text,
        entity_ctx=entity_ctx,
        intent_label=intent_label,
        trace=trace,
        last_ai=preparation.last_ai,
        history=history,
        definition_term=preparation.definition_term,
        profile=collected["profile"],
        lessons=collected["lessons"],
        concerns=collected["concerns"],
        jargon=collected["jargon"],
        style_examples=collected["style_examples"],
        facts=collected["facts"],
        behavior=collected["behavior"],
        goals_text=collected["goals_text"],
        open_issues=collected["open_issues"],
        slang=collected["slang"],
        mood=collected["mood"],
        mood_state=collected["mood_state"],
        self_state=collected["self_state"],
        older=collected["older"],
        extra_blocks=collected["extra_blocks"],
    )
