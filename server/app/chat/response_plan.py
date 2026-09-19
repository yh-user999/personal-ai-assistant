"""统一响应策略：把消息分成事实、检索、推理、创作、情绪和行动模式。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.chat import values as values_module
from app.common.timeutil import now_local

_ALLOWED_PROVIDERS = frozenset({"current_datetime", "calculator", "web_search", "hotboard"})
_ALLOWED_ROUTES = frozenset({"direct", "local_memory", "web_research", "hybrid", "domain"})
_RESEARCH_KINDS = frozenset({"novel", "knowledge", "document", "project", "news", "url"})
_CONTEXT_RELATIONS = frozenset({"new_topic", "continues_previous", "unclear"})
_CONTEXT_CLOSE_REASONS = frozenset({"", "non_continuation", "planner_ignore", "window_exhausted", "expired"})
_REPLY_DECISIONS = frozenset({"ignore", "answer", "ask_back", "interject"})
SOCIAL_ACTIONS = frozenset({"ignore", "interject", "banter", "answer", "tease", "ask_back"})
_HIGH_RISK_WORDS = ("删除", "执行", "运行", "发送", "修改生产", "改配置")
_SOCIAL_SWITCH_RE = re.compile(r"换个话题|另外|顺便(?:问|说)|对了|再问一个|先不说|不聊这个了")
_SOCIAL_BANTER_RE = re.compile(r"哈哈+|笑死|绷不住|绝了|离谱|好家伙|太真实|破防|6{2,}")
_SOCIAL_TEASE_RE = re.compile(r"你又|还在|这都|不会吧|真有你的|又来了")
_SOCIAL_ASK_BACK_RE = re.compile(r"你觉得呢|你说呢|怎么办|咋办|怎么选|选哪个|要不要")
_SOCIAL_QUESTION_RE = re.compile(r"[?？]|吗[？?。！!\s]*$|(?:怎么|为什么|是否|能不能|有没有|哪个|哪些|什么)")
_SOCIAL_EMOTION_RE = re.compile(r"焦虑|难受|崩溃|烦|累|沮丧|生气|压力|睡不着|没劲|撑不住|委屈")

# 无来源不判断：价值层的安全底线，必须先于任何道德/事实结论生效
NO_SOURCE_RULE = "本轮没有任何检索来源时必须回答「未查到」，不得凭模型记忆作答或补细节"

MODES = frozenset({
    "direct_fact", "retrieve_then_answer", "reasoning", "creative",
    "emotional_support", "action", "clarify", "refuse_or_confirm", "casual_chat",
    "moral_assessment",
})

# 道德评价模式的固定约束：事实与判断分离、必须表态、不越界、不和稀泥
MORAL_CONSTRAINTS = (
    "按三层组织回答：已确认事实 / 说法不一致之处 / 我的判断",
    "价值底线稳定，个案事实可修正：关键行为查明时明确表态；未知不代表双方责任相等",
    "主判断依据具体行为、能力义务、必要性与比例；合理感受作从属澄清，不抵消不当行为",
    "按证据分别评价责任，双方确有不同过错时分别指出；不得无依据地各打五十大板，"
    "也不得为了鲜明立场强行只批评一方",
    "区分「摆平」与「摆对」：不得用「已调解/已赔付/程序走完」当道德是非的"
    "结论或背书；程序结果不代表事情就对了",
    "当受伤方、被指控方、弱势方是同一人时，不得用「对等纠纷」框架叙述，"
    "要点出事实上的不对等",
    "不得把传播量、情绪强度、措辞激烈程度当作依据",
    "不得对未确认事实作定性，不代替司法定罪或医学诊断",
    "判断强度取决于关键事实的直接证据、出处独立性、时间完整性及反证；"
    "媒体名气或转载数量不能自动证明事实，只有归属可核对时应按来源声称来表达",
)
SENSITIVE_CONSTRAINTS = (
    "涉及未成年人等敏感主体：不展开可识别身份细节",
    "不推测当事人动机，以官方通报为准",
)

# 外部事件线索：道德评价里出现具体主体（地名/年龄/称谓/"这件事/这起"），
# 说明是在评价一个真实事件而非抽象伦理，应先检索取证。
_EXTERNAL_EVENT_RE = re.compile(
    r"[\u4e00-\u9fa5]{2,3}(?:省|市|县|区|镇|村)|"          # 地名
    r"\d{1,3}\s*岁|"                                        # 年龄
    r"(?:男童|女童|幼童|男孩|女孩|小孩|男子|女子|老人|学生|网红|博主)|"  # 称谓
    r"这(?:个|名)?(?:男的|女的|人)|"                        # 口语称谓
    r"这(?:件|起|条|个)事|该事件|此事|这事"                  # 指代具体事件
)


def _refers_external_event(text: str) -> bool:
    return bool(_EXTERNAL_EVENT_RE.search(text or ""))


_TIME_RE = re.compile(r"(?:几点|现在时间|当前时间|什么时间|现在是几号|今天(?:是)?(?:星期|周|礼拜)[一二三四五六日天几]|今天(?:是)?(?:几号|几月几号)|当前日期(?:是什么)?|今天日期)")
_CREATIVE_RE = re.compile(r"继续写|接着写|续写|改写|润色|写一段|设计剧情|头脑风暴|起个名字")
_EMOTION_RE = re.compile(r"焦虑|难受|崩溃|烦|累|沮丧|生气|压力|睡不着|没劲|撑不住")
_ACTION_RE = re.compile(r"帮我(?:打开|删除|执行|运行|修改|移动|复制|重命名|发送)|设置提醒|记录(?:一下|：)|删除|执行脚本|运行脚本")
_RECALL_RE = re.compile(r"我之前|上次|以前|记得吗|你还记得|我说过|设定是什么|原文|翻出来")
_REASONING_RE = re.compile(r"为什么|怎么判断|有什么问题|怎么选|对比一下|分析一下|方案|架构|是否合理|优缺点")


@dataclass
class ResponsePlan:
    mode: str = "casual_chat"
    intent: str = "general_chat"
    confidence: float = 0.0
    evidence_required: bool = False
    retrieval_required: bool = False
    tool_required: bool = False
    confirmation_required: bool = False
    tone: str = "natural"
    constraints: list[str] = field(default_factory=list)
    source: str = "fallback"
    fact_result: dict[str, Any] | None = None
    provider: str | None = None
    query: str | None = None
    route: str = "direct"
    research_kind: str | None = None
    subject: str = ""
    research_question: str = ""
    research_queries: list[str] = field(default_factory=list)
    source_preference: list[str] = field(default_factory=list)
    followup_kind: str = ""
    followup_required: bool = False
    context_relation: str = "new_topic"
    context_continue: bool = False
    context_subject: str = ""
    context_close_reason: str = ""
    reply_decision: str = "answer"
    analysis_only: bool = False
    action: str | None = None
    risk: str = "low"
    needs_clarification: bool = False
    reason: str = ""
    needs_moral_judgment: bool = False
    sensitive_subject: bool = False
    stance_required: bool = True
    investigation_required: bool = False
    # 群聊社交动作与事实/工具响应策略正交：只控制是否/如何接话，不能授予权限。
    social_action: str = ""
    social_confidence: float = 0.0
    social_reasons: list[str] = field(default_factory=list)
    social_addressed: bool = False
    social_atmosphere: str = "casual"
    social_topic_shift: bool = False
    social_score: float = 0.0
    social_factors: dict[str, float] = field(default_factory=dict)
    social_penalties: dict[str, float] = field(default_factory=dict)
    social_gate_reason: str = ""
    social_gate_allowed: bool = False
    social_gate_would_allow: bool = False
    social_cooldown_remaining: float = 0.0
    social_hourly_count: int = 0
    social_message_gap: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "intent": self.intent,
            "confidence": round(max(0.0, min(1.0, self.confidence)), 3),
            "evidence_required": self.evidence_required,
            "retrieval_required": self.retrieval_required,
            "tool_required": self.tool_required,
            "confirmation_required": self.confirmation_required,
            "needs_clarification": self.needs_clarification,
            "provider": self.provider,
            "route": self.route,
            "research_kind": self.research_kind,
            "subject": self.subject[:160],
            "research_question": self.research_question[:400],
            "research_queries": list(self.research_queries)[:3],
            # 兼容 planner/观测端使用的简短字段名；执行层使用上面的有界字段。
            "question": self.research_question[:400],
            "queries": list(self.research_queries)[:3],
            "source_preference": list(self.source_preference)[:5],
            "followup_kind": self.followup_kind[:32],
            "followup_required": bool(self.followup_required),
            "context_relation": self.context_relation,
            "context_continue": bool(self.context_continue),
            "context_subject": self.context_subject[:160],
            "context_close_reason": self.context_close_reason[:40],
            "reply_decision": self.reply_decision,
            "analysis_only": bool(self.analysis_only),
            "action": self.action,
            "risk": self.risk,
            "tone": self.tone,
            "source": self.source,
            "query": self.query,
            "needs_moral_judgment": self.needs_moral_judgment,
            "sensitive_subject": self.sensitive_subject,
            "stance_required": self.stance_required,
            "investigation_required": self.investigation_required,
            "social_action": self.social_action or None,
            "social_confidence": round(max(0.0, min(1.0, self.social_confidence)), 3),
            "social_reasons": list(self.social_reasons)[:6],
            "social_addressed": self.social_addressed,
            "social_atmosphere": self.social_atmosphere[:32],
            "social_topic_shift": bool(self.social_topic_shift),
            "social_score": round(max(0.0, min(1.0, self.social_score)), 3),
            "social_factors": {key: round(max(0.0, min(1.0, float(value))), 3) for key, value in self.social_factors.items()},
            "social_penalties": {key: round(max(0.0, min(1.0, float(value))), 3) for key, value in self.social_penalties.items()},
            "social_gate_reason": self.social_gate_reason[:80],
            "social_gate_allowed": bool(self.social_gate_allowed),
            "social_gate_would_allow": bool(self.social_gate_would_allow),
            "social_cooldown_remaining": round(max(0.0, self.social_cooldown_remaining), 3),
            "social_hourly_count": max(0, int(self.social_hourly_count)),
            "social_message_gap": max(0, int(self.social_message_gap)),
            # constraints 必须带上：提示词靠它注入模式约束。
            # 漏掉会让道德/无来源/动作等约束全部静默失效。
            "constraints": list(self.constraints),
        }


def build_group_social_hint(
    message: str,
    history: list[dict[str, Any]] | None = None,
    *,
    addressed: bool = True,
    interject_enabled: bool = False,
    scene: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """根据群聊消息与有限场景上下文生成可审计的社交动作提示。

    规则只负责安全默认值与明显信号；最终动作可由现有 semantic planner 选择，
    但调用方仍须经过 ``apply_group_social_requirements`` 的硬门禁。
    """
    text = str(message or "").strip()
    recent = history or []
    scene = scene or {}
    reasons: list[str] = []
    atmosphere = str(scene.get("atmosphere") or "casual").strip()[:32] or "casual"

    if _SOCIAL_EMOTION_RE.search(text):
        atmosphere = "emotional"
        reasons.append("emotion_signal")
    elif _SOCIAL_BANTER_RE.search(text):
        atmosphere = "casual"
        reasons.append("banter_signal")
    elif _SOCIAL_QUESTION_RE.search(text):
        atmosphere = "questioning"
    if _SOCIAL_QUESTION_RE.search(text) and "question_signal" not in reasons:
        reasons.append("question_signal")
    if _SOCIAL_SWITCH_RE.search(text):
        reasons.append("topic_switch")
    if recent and any(str(item.get("role") or "") == "assistant" for item in recent[-4:]):
        reasons.append("recent_bot_context")
    if scene.get("topic_shift"):
        reasons.append("scene_topic_shift")

    if not addressed:
        if not interject_enabled:
            action = "ignore"
            confidence = 1.0
            reasons.append("not_directed")
            reasons.append("interject_disabled")
        elif _SOCIAL_QUESTION_RE.search(text) or _SOCIAL_BANTER_RE.search(text):
            action = "interject"
            confidence = 0.55
            reasons.append("candidate_interjection")
        else:
            action = "ignore"
            confidence = 0.85
            reasons.append("not_directed")
    elif _SOCIAL_ASK_BACK_RE.search(text):
        action = "ask_back"
        confidence = 0.72
        reasons.append("needs_choice_or_clarification")
    elif _SOCIAL_TEASE_RE.search(text):
        action = "tease"
        confidence = 0.58
        reasons.append("teasing_signal")
    elif _SOCIAL_BANTER_RE.search(text):
        action = "banter"
        confidence = 0.68
    else:
        action = "answer"
        confidence = 0.9 if addressed else 0.4

    return {
        "action": action,
        "confidence": confidence,
        "reasons": list(dict.fromkeys(reasons))[:6],
        "atmosphere": atmosphere,
        "addressed": bool(addressed),
    }


def apply_group_social_requirements(
    plan: ResponsePlan,
    *,
    message: str,
    history: list[dict[str, Any]] | None = None,
    addressed: bool = True,
    context_active: bool = False,
    enabled: bool = True,
    interject_enabled: bool = False,
    min_confidence: float = 0.6,
    scene: dict[str, Any] | None = None,
) -> ResponsePlan:
    """把群聊社交规则合并到 planner 结果，并执行不可绕过的安全边界。"""
    if not enabled:
        plan.social_action = ""
        plan.social_confidence = 0.0
        plan.social_reasons = []
        plan.social_addressed = bool(addressed)
        plan.social_atmosphere = "casual"
        plan.social_topic_shift = False
        return plan

    effective_addressed = bool(addressed or context_active)
    hint = build_group_social_hint(
        message,
        history,
        addressed=effective_addressed,
        interject_enabled=interject_enabled,
        scene=scene,
    )
    planner_social = plan.social_action in SOCIAL_ACTIONS and plan.source == "llm"
    action = plan.social_action if plan.social_action in SOCIAL_ACTIONS else hint["action"]
    confidence = plan.social_confidence if plan.social_action in SOCIAL_ACTIONS else hint["confidence"]
    reasons = list(plan.social_reasons) if plan.social_action in SOCIAL_ACTIONS else []
    reasons.extend(hint["reasons"])
    atmosphere = (
        str(plan.social_atmosphere or "").strip()[:32]
        if planner_social and plan.social_confidence >= min_confidence
        else str(hint.get("atmosphere") or "casual")[:32]
    ) or "casual"

    # 直接唤醒或窗口内续话是现有用户契约：不能被模型的社交判断静默掉。
    if effective_addressed and action in {"ignore", "interject"}:
        action = "answer"
        confidence = max(confidence, 0.9)
        reasons.append(
            "directed_or_context_must_answer" if context_active else "directed_must_answer"
        )
    # 非直接、非窗口消息在主动插话开关关闭时严格沉默。
    if not effective_addressed and not interject_enabled:
        action = "ignore"
        confidence = 1.0
        reasons.extend(("not_directed", "interject_disabled"))
    # 低置信的模型动作回退到规则结果；直接/窗口消息仍以回答为最低保障。
    if confidence < max(0.0, min(1.0, float(min_confidence))):
        action = hint["action"]
        confidence = hint["confidence"]
        reasons.append("low_confidence_fallback")
        if effective_addressed and action in {"ignore", "interject"}:
            action = "answer"
            reasons.append(
                "directed_or_context_must_answer" if context_active else "directed_must_answer"
            )

    plan.social_action = action
    plan.social_confidence = max(0.0, min(1.0, float(confidence)))
    plan.social_reasons = list(dict.fromkeys(str(item)[:80] for item in reasons if str(item).strip()))[:6]
    plan.social_addressed = bool(addressed)
    plan.social_atmosphere = atmosphere
    plan.social_topic_shift = bool(
        (scene and scene.get("topic_shift")) or _SOCIAL_SWITCH_RE.search(str(message or ""))
    )
    return plan


def _previous_user_text(history: list[dict[str, Any]] | None) -> str:
    """历史里最近一条用户消息（本轮不在其中）。"""
    for item in reversed(history or []):
        if str(item.get("role") or "") != "user":
            continue
        text = str(item.get("content") or "").strip()
        if text:
            return text
    return ""


def is_investigation_followup(message: str) -> bool:
    """只识别续查指令；必须另有相邻调查摘要才继承，不能凭空指定事件。"""
    from app.chat import web_provider

    text = (message or "").strip()
    if re.fullmatch(r"(?:继续|展开|详细说|多说点)[？?。！!\s]*", text):
        return False  # 裸的继续可能是在请求解释/创作，不自动升级为新一轮调查。
    return web_provider.looks_like_followup(text) or bool(re.fullmatch(
        r"(?:你)?(?:再|继续)(?:查查|查证|核实|查清楚|搜一下|看看有没有其他新闻来源)"
        r"[吧呢吗？?。！!\s]*|还有其他(?:新闻)?来源吗[？?。\s]*", text
    ))


def build_rule_plan(
    message: str,
    *,
    is_owner: bool = True,
    history: list[dict[str, Any]] | None = None,
    investigation_context: dict[str, Any] | None = None,
) -> ResponsePlan:
    text = (message or "").strip()
    if not text:
        return ResponsePlan()
    if _TIME_RE.search(text):
        return ResponsePlan(
            mode="direct_fact", intent="current_datetime", confidence=0.99,
            evidence_required=True, tool_required=True, tone="brief",
            constraints=["必须使用当前时间 provider，不得凭模型记忆猜测"], source="rule",
        )
    from app.chat import web_provider, web_research

    prior = investigation_context or {}
    if is_owner and prior.get("question") and is_investigation_followup(text):
        return ResponsePlan(
            mode="retrieve_then_answer", intent="event_followup", confidence=0.9,
            evidence_required=True, retrieval_required=True, tool_required=True,
            provider="web_search", query=str(prior["question"])[:400],
            investigation_required=True, constraints=list(MORAL_CONSTRAINTS) + [NO_SOURCE_RULE],
            source="rule",
        )
    if web_provider.looks_like_external_reference_lookup(text):
        _, expanded_query = web_provider.reference_search_queries(text)
        return ResponsePlan(
            mode="retrieve_then_answer", intent="external_reference_lookup", confidence=0.92,
            evidence_required=True, retrieval_required=True, tool_required=True,
            provider="web_search", route="web_research", research_kind="novel",
            subject=web_provider._reference_title_text(text)[:160],
            research_question=text[:400], research_queries=[expanded_query or text[:400]],
            query=(expanded_query or text[:400]),
            constraints=[
                NO_SOURCE_RULE,
                "只能依据本轮检索来源概括，来源不足时明确说明无法核实",
                "主观评价必须和来源支持的事实分开，不得凭模型记忆补写剧情或口碑",
            ],
            source="rule",
        )
    fallback_kind = web_research.classify_query(text)
    if fallback_kind in {"project", "document", "url", "knowledge"}:
        return ResponsePlan(
            mode="retrieve_then_answer", intent=f"{fallback_kind}_lookup", confidence=0.9,
            evidence_required=True, retrieval_required=True, tool_required=True,
            provider="web_search", route="web_research", research_kind=fallback_kind,
            subject=text[:160], research_question=text[:400], research_queries=[text[:400]],
            query=text[:400],
            constraints=[NO_SOURCE_RULE, "只能依据本轮检索来源回答，并保留来源链接"],
            source="rule",
        )
    if web_provider.looks_like_hot_browsing(text):
        # 浏览型："最近有什么大事" → 热榜（当下热议话题清单），非关键词检索
        return ResponsePlan(
            mode="retrieve_then_answer",
            intent="hot_browsing",
            confidence=0.85,
            evidence_required=True, retrieval_required=True, tool_required=True,
            provider="hotboard", query=text[:120],
            constraints=[
                "热榜是当下热议话题清单，不是已核实事实，概述时须说明具体情况需进一步核实",
            ],
            source="rule",
        )
    if web_provider.needs_web_search(text):
        # 时效/事件类问题必须联网取证；没有来源时只能说"未查到"。
        event = web_provider.looks_like_event_query(text)
        return ResponsePlan(
            mode="retrieve_then_answer",
            intent="event_lookup" if event else "latest_news",
            confidence=0.9,
            evidence_required=True, retrieval_required=True, tool_required=True,
            provider="web_search", route="web_research", research_kind="news",
            subject=text[:160], research_question=text[:400], research_queries=[text[:400]],
            query=text[:400], investigation_required=event,
            constraints=[NO_SOURCE_RULE, "回答须标注来源与发布时间", "报道措辞不构成事实或道德依据"],
            source="rule",
        )
    # 指代式追问（"现在呢""后来呢"）：新闻是**会变的事实**，用户要的是新进展，
    # 不是把上一轮的报道复述一遍。实测这一问的 provider 为空、直接跳过检索。
    # query 用上一轮的话题词——追问本身（"现在呢"）当检索词毫无价值。
    previous = _previous_user_text(history)
    if web_provider.needs_web_search_after_followup(text, previous):
        return ResponsePlan(
            mode="retrieve_then_answer",
            intent="latest_news",
            confidence=0.8,
            evidence_required=True, retrieval_required=True, tool_required=True,
            provider="web_search", route="web_research", research_kind="news",
            subject=previous[:160], research_question=text[:400], research_queries=[previous[:400]],
            query=previous[:400],
            constraints=[NO_SOURCE_RULE, "回答须标注来源与发布时间", "报道措辞不构成事实或道德依据"],
            source="rule",
        )
    if values_module.looks_like_judgment_request(text):
        sensitive = values_module.is_sensitive_subject(text)
        constraints = list(MORAL_CONSTRAINTS)
        if sensitive:
            constraints.extend(SENSITIVE_CONSTRAINTS)
        # 若这是"对某个外部事件的道德评价"（提到新闻/事件/具体主体），
        # 必须先检索取证再判断——否则会像这次一样凭旧记忆下结论、漏掉关键事实
        # （"勒颈""后退"都是没检索就说不出的）。纯抽象伦理问题（该不该惩罚）
        # 不联网，保持 provider 为空。
        about_event = (
            web_provider.needs_web_search(text)
            or web_provider.looks_like_event_query(text)
            or _refers_external_event(text)
        )
        return ResponsePlan(
            mode="moral_assessment", intent="moral_judgment", confidence=0.85,
            evidence_required=True, retrieval_required=True,
            tool_required=about_event,
            provider="web_search" if about_event else None,
            route="web_research" if about_event else "direct",
            research_kind="news" if about_event else None,
            subject=text[:160] if about_event else "",
            research_question=text[:400] if about_event else "",
            research_queries=[text[:400]] if about_event else [],
            investigation_required=about_event,
            query=text[:400] if about_event else "",
            needs_moral_judgment=True, sensitive_subject=sensitive,
            constraints=constraints, source="rule",
        )
    if _ACTION_RE.search(text):
        return ResponsePlan(
            mode="action" if is_owner else "refuse_or_confirm", intent="requested_action",
            confidence=0.9, tool_required=True, confirmation_required=True,
            constraints=["必须基于实际执行结果回答，不能假装已完成"], source="rule",
        )
    if _CREATIVE_RE.search(text):
        return ResponsePlan(
            mode="creative", intent="creative_generation", confidence=0.88,
            retrieval_required=True, tone="natural",
            constraints=["遵守已确认设定，不把创作内容说成现实事实"], source="rule",
        )
    if _RECALL_RE.search(text):
        return ResponsePlan(
            mode="retrieve_then_answer", intent="memory_recall", confidence=0.86,
            evidence_required=True, retrieval_required=True,
            constraints=["区分原文、持久事实、推断和不确定内容"], source="rule",
        )
    if _EMOTION_RE.search(text):
        return ResponsePlan(
            mode="emotional_support", intent="emotional_support", confidence=0.82,
            retrieval_required=True, tone="warm",
            constraints=["先回应当前状态，不强行堆建议"], source="rule",
        )
    if _REASONING_RE.search(text):
        return ResponsePlan(
            mode="reasoning", intent="analysis", confidence=0.78,
            evidence_required=True, retrieval_required=True,
            constraints=["区分已知事实与推断，给出判断依据"], source="rule",
        )
    return ResponsePlan(mode="casual_chat", intent="general_chat", confidence=0.55, source="fallback")


def _text_list(value: Any, limit: int = 6) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:160] for item in value if str(item).strip()][:limit]


def parse_llm_plan(
    text: str,
    *,
    is_owner: bool = True,
    min_confidence: float = 0.60,
    fallback: ResponsePlan | None = None,
) -> ResponsePlan:
    """解析 LLM 策略 JSON；非法或低置信计划安全回退，不执行动作。

    ``fallback`` 应传规则计划：解析失败时退回它，而不是退成裸的 casual_chat——
    后者会把规则层已经判定的取证要求（如时效问题必须联网）一并丢掉，
    表现为"模型没规划好，于是连该查的都不查了"。
    """
    from app.chat import llm_json

    raw = llm_json.extract_json_object(text)
    if raw is None:
        llm_json.log_unparsed("响应 planner", text)
        return fallback or ResponsePlan(mode="casual_chat", intent="planner_fallback", source="fallback")
    try:
        mode = str(raw.get("mode") or "").strip()
        intent = str(raw.get("intent") or "general_chat").strip()[:80]
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.0))))
        if mode not in MODES or confidence < min_confidence:
            if fallback is not None:
                return fallback
            return ResponsePlan(mode="clarify" if confidence > 0.3 else "casual_chat", intent="uncertain", confidence=confidence, source="fallback", needs_clarification=confidence > 0.3)
        provider = str(raw.get("provider") or "").strip() or None
        if provider not in _ALLOWED_PROVIDERS:
            provider = None
        route = str(raw.get("route") or "direct").strip().lower()
        if route not in _ALLOWED_ROUTES:
            route = "direct"
        research_kind = str(raw.get("research_kind") or "").strip().lower() or None
        if research_kind not in _RESEARCH_KINDS:
            research_kind = None
        research_queries = _text_list(
            raw.get("research_queries") if raw.get("research_queries") is not None else raw.get("queries"),
            limit=3,
        )
        source_preference = _text_list(raw.get("source_preference"), limit=5)
        followup_kind = str(raw.get("followup_kind") or "").strip()[:32]
        followup_required = raw.get("followup_required") is True
        context_relation = str(raw.get("context_relation") or "new_topic").strip().lower()
        if context_relation not in _CONTEXT_RELATIONS:
            context_relation = "unclear"
        context_continue = raw.get("context_continue") is True
        context_subject = str(raw.get("context_subject") or "").strip()[:160]
        context_close_reason = str(raw.get("context_close_reason") or "").strip()[:40]
        if context_close_reason not in _CONTEXT_CLOSE_REASONS:
            context_close_reason = ""
        reply_decision = str(raw.get("reply_decision") or "answer").strip().lower()
        if reply_decision not in _REPLY_DECISIONS:
            reply_decision = "ignore"
        analysis_only = raw.get("analysis_only") is True
        risk = str(raw.get("risk") or "low").strip().lower()
        if risk not in {"low", "medium", "high"}:
            risk = "low"
        action = str(raw.get("action") or "").strip()[:80] or None
        if any(word in (intent + " " + (action or "")) for word in _HIGH_RISK_WORDS):
            risk = "high"
        raw_social_action = str(raw.get("social_action") or "").strip()
        social_action = raw_social_action if raw_social_action in SOCIAL_ACTIONS else ""
        try:
            social_confidence = max(0.0, min(1.0, float(raw.get("social_confidence", 0.0))))
        except (TypeError, ValueError):
            social_confidence = 0.0
        raw_social_addressed = raw.get("social_addressed")
        social_addressed = bool(raw_social_addressed) if isinstance(raw_social_addressed, bool) else False
        social_atmosphere = str(raw.get("social_atmosphere") or "casual").strip()[:32] or "casual"
        social_topic_shift = raw.get("social_topic_shift") is True
        plan = ResponsePlan(
            mode=mode,
            intent=intent,
            confidence=confidence,
            evidence_required=bool(raw.get("evidence_required")),
            retrieval_required=bool(raw.get("retrieval_required")),
            tool_required=bool(raw.get("tool_required")),
            confirmation_required=bool(raw.get("confirmation_required")),
            needs_clarification=bool(raw.get("needs_clarification")),
            tone=str(raw.get("tone") or "natural").strip()[:40] or "natural",
            constraints=_text_list(raw.get("constraints")),
            source="llm",
            provider=provider,
            query=str(raw.get("query") or "").strip()[:500] or None,
            route=route,
            research_kind=research_kind,
            subject=str(raw.get("subject") or "").strip()[:160],
            research_question=str(
                raw.get("research_question") if raw.get("research_question") is not None
                else raw.get("question") or ""
            ).strip()[:400],
            research_queries=research_queries,
            source_preference=source_preference,
            followup_kind=followup_kind,
            followup_required=followup_required,
            context_relation=context_relation,
            context_continue=context_continue,
            context_subject=context_subject,
            context_close_reason=context_close_reason,
            reply_decision=reply_decision,
            analysis_only=analysis_only,
            action=action,
            risk=risk,
            reason=str(raw.get("reason") or "").strip()[:120],
            investigation_required=raw.get("investigation_required") is True,
            needs_moral_judgment=raw.get("needs_moral_judgment") is True,
            sensitive_subject=raw.get("sensitive_subject") is True,
            social_action=social_action,
            social_confidence=social_confidence,
            social_reasons=_text_list(raw.get("social_reasons")),
            social_addressed=social_addressed,
            social_atmosphere=social_atmosphere,
            social_topic_shift=social_topic_shift,
        )
        return validate_plan(plan, is_owner=is_owner)
    except (TypeError, ValueError, KeyError) as exc:
        import logging

        logging.getLogger("assistant.chat.response_plan").warning(
            "响应计划字段非法（%s），退回规则计划", type(exc).__name__
        )
        return fallback or ResponsePlan(mode="casual_chat", intent="planner_fallback", source="fallback")


def apply_rule_requirements(plan: ResponsePlan, hint: ResponsePlan, *, is_owner: bool = True) -> ResponsePlan:
    """把规则层判定的**取证要求**并回 planner 的计划。

    规则与 planner 分工不同：planner 决定"怎么答更合适"，规则决定"什么必须
    核实"。时效/事件类问题必须联网取证属于后者，不能被 planner 的
    casual_chat 或低置信结果取消——生产上就是这样漏掉了一次该做的检索。

    只合并强制项，不把规则的全部约束无差别叠加，避免把 planner 的判断
    覆盖成规则模板。
    """
    hard_web_hint = hint.provider == "web_search" and (
        hint.investigation_required
        or hint.needs_moral_judgment
        or hint.intent in {"latest_news", "event_lookup", "event_followup"}
    )
    # 语义 planner 成功后，旧的作品/资料关键词规则不再夺取联网决策；
    # 只有时效事实、事件核查和道德判断这类安全要求可以强制取证。
    # planner 失败时 plan.source 不是 llm，完整规则计划仍作为兼容 fallback。
    if hint.provider == "web_search" and (hard_web_hint or plan.source != "llm"):
        plan.route = "web_research"
        plan.provider = "web_search"
        plan.tool_required = True
        plan.evidence_required = True
        plan.retrieval_required = True
        if not plan.research_kind:
            plan.research_kind = hint.research_kind
        if plan.mode in {"casual_chat", "clarify"}:
            plan.mode = "retrieve_then_answer"
            plan.intent = hint.intent
        if not plan.query:
            plan.query = hint.query
        for item in hint.constraints:
            if item not in plan.constraints:
                plan.constraints.append(item)
    if hint.provider == "hotboard" and (plan.source != "llm" or plan.intent == "hot_browsing"):
        # 旧 hotboard 规则仅作为 planner 不可用时的 fallback；
        # planner 明确选择 hot_browsing 时只补齐 provider，不覆盖其它语义字段。
        plan.provider = "hotboard"
        plan.tool_required = True
        plan.retrieval_required = True
        if plan.mode in {"casual_chat", "clarify"}:
            plan.mode = "retrieve_then_answer"
        plan.intent = "hot_browsing"
        if not plan.query:
            plan.query = hint.query
        for item in hint.constraints:
            if item not in plan.constraints:
                plan.constraints.append(item)
    if hint.investigation_required and plan.provider == "web_search":
        plan.investigation_required = True
    if hint.needs_moral_judgment:
        plan.needs_moral_judgment = True
        plan.evidence_required = True
        plan.sensitive_subject = plan.sensitive_subject or hint.sensitive_subject
        for item in hint.constraints:
            if item not in plan.constraints:
                plan.constraints.append(item)
    return validate_plan(plan, is_owner=is_owner)


def validate_plan(plan: ResponsePlan, *, is_owner: bool = True) -> ResponsePlan:
    """代码强制安全边界：planner 不能授予权限或执行未知工具。"""
    if plan.context_relation not in _CONTEXT_RELATIONS:
        plan.context_relation = "unclear"
    if plan.context_close_reason not in _CONTEXT_CLOSE_REASONS:
        plan.context_close_reason = ""
    if plan.reply_decision not in _REPLY_DECISIONS:
        plan.reply_decision = "ignore"
    if plan.analysis_only:
        plan.reply_decision = "ignore"
    if plan.route not in _ALLOWED_ROUTES:
        plan.route = "direct"
    if plan.research_kind not in _RESEARCH_KINDS:
        plan.research_kind = None
    if plan.provider and plan.provider not in _ALLOWED_PROVIDERS:
        plan.provider = None
        plan.tool_required = False
    if plan.route in {"web_research", "hybrid"}:
        plan.provider = "web_search"
        plan.tool_required = True
        plan.retrieval_required = True
        plan.evidence_required = True
        if not plan.research_kind:
            plan.research_kind = "knowledge"
        if plan.mode in {"casual_chat", "clarify"}:
            plan.mode = "retrieve_then_answer"
        if not plan.query:
            plan.query = (
                plan.research_queries[0]
                if plan.research_queries else plan.research_question or plan.subject or None
            )
        if NO_SOURCE_RULE not in plan.constraints:
            plan.constraints.append(NO_SOURCE_RULE)
    elif plan.route == "local_memory":
        plan.retrieval_required = True
    elif plan.provider == "web_search":
        plan.route = "web_research"
        plan.tool_required = True
        plan.retrieval_required = True
        plan.evidence_required = True
        if not plan.research_kind:
            plan.research_kind = "knowledge"
        if not plan.query:
            plan.query = (
                plan.research_queries[0]
                if plan.research_queries else plan.research_question or plan.subject or None
            )
        if NO_SOURCE_RULE not in plan.constraints:
            plan.constraints.append(NO_SOURCE_RULE)
    if plan.mode == "action":
        if not is_owner:
            return ResponsePlan(mode="refuse_or_confirm", intent=plan.intent, confidence=plan.confidence, source="fallback", risk="high", confirmation_required=True)
        plan.confirmation_required = True
    if plan.risk == "high":
        plan.confirmation_required = True
        if plan.mode != "action":
            plan.mode = "refuse_or_confirm"
    if plan.tool_required and not plan.provider and plan.mode == "direct_fact":
        plan.mode = "clarify"
        plan.needs_clarification = True
    if plan.mode == "direct_fact" and not plan.provider:
        plan.mode = "clarify"
        plan.needs_clarification = True
    if plan.mode == "clarify":
        plan.confirmation_required = False
    if plan.mode == "moral_assessment":
        # 道德判断必须建立在事实上，且核心是非必须表态
        plan.needs_moral_judgment = True
        plan.evidence_required = True
        plan.stance_required = True
        for item in MORAL_CONSTRAINTS:
            if item not in plan.constraints:
                plan.constraints.append(item)
        if plan.sensitive_subject:
            for item in SENSITIVE_CONSTRAINTS:
                if item not in plan.constraints:
                    plan.constraints.append(item)
    if plan.provider == "web_search" and plan.needs_moral_judgment:
        plan.investigation_required = True
    if not is_owner or plan.provider != "web_search":
        plan.investigation_required = False
    return plan


def build_planner_messages(
    message: str, history: list[dict[str, Any]], rule_hint: ResponsePlan,
    investigation_context: dict[str, Any] | None = None,
    *,
    is_group: bool = False,
    group_directed: bool = True,
    group_context_active: bool = False,
    group_scene: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    import json

    schema = {
        "route": "direct",
        "research_kind": None,
        "subject": "",
        "research_question": "",
        "research_queries": [],
        "source_preference": [],
        "followup_kind": "",
        "followup_required": False,
        "context_relation": "new_topic",
        "context_continue": False,
        "context_subject": "",
        "context_close_reason": "",
        "reply_decision": "answer",
        "analysis_only": False,
        "intent": "general_chat",
        "mode": "casual_chat",
        "confidence": 0.0,
        "evidence_required": False,
        "retrieval_required": False,
        "tool_required": False,
        "confirmation_required": False,
        "needs_clarification": False,
        "provider": None,
        "query": None,
        "investigation_required": False,
        "needs_moral_judgment": False,
        "action": None,
        "risk": "low",
        "tone": "natural",
        "constraints": [],
    }
    if is_group:
        schema.update({
            "social_action": "answer" if group_directed else "ignore",
            "social_confidence": 0.0,
            "social_reasons": [],
            "social_addressed": bool(group_directed),
        })
    context = [{"role": item.get("role", "user"), "content": str(item.get("content", ""))[:500]} for item in history[-4:]]
    social_rules = ""
    if is_group:
        social_rules = (
            "这是群聊。除非 social_action 明确允许，否则不要抢话；可选 social_action: "
            "ignore/interject/banter/answer/tease/ask_back。"
            "明确 @、前缀或回复机器人时不得选择 ignore/interject，至少选择 answer。"
            "tease 只能针对当前话题或行为轻微吐槽，不得攻击个人敏感信息。"
            f"本轮是否明确对机器人说话：{bool(group_directed)}。"
            f"本轮是否处于 @ 后短时上下文窗口：{bool(group_context_active)}。"
            "窗口内必须额外判断 context_relation、context_continue、reply_decision 和 analysis_only："
            "只有明确继续上一话题且 reply_decision 为 answer/ask_back 才生成回复；"
            "换题、无关或不确定时可只分析并静默，不要凭关键词强行承接。"
        )
    return [
        {"role": "system", "content": (
            "你是私人助手的响应策略规划器。不要回答用户，只返回 JSON。"
            "自主判断用户意图和最佳响应模式，但不能授予权限、执行动作或编造事实。"
            + social_rules
            + "可选 route: direct/local_memory/web_research/hybrid/domain；可选 mode: "
            + ", ".join(sorted(MODES)) + "。"
            "route 表示本轮主要信息路径：direct 是普通对话，local_memory 是查本地记忆，"
            "web_research 是查公开网页/GitHub/URL，hybrid 是本地记忆与公开资料结合，domain 是调用已有领域服务。"
            "research_kind 只在 web_research/hybrid 时填写 novel/knowledge/document/project/news/url。"
            "不要按固定关键词机械判断；根据用户真正想要的事实、资料、近况或作品信息理解语义。"
            "例如‘你知道《没钱修什么仙》吗？’应理解为 novel web_research；"
            "‘我最近状态怎么样’应理解为 local_memory；‘你好’应理解为 direct。"
            "涉及外部世界的时效信息、新闻、事件、公共人物/机构/地区/公司的近况与进展、"
            "政策或数据当前值时，provider 填 web_search，query 填用户原话或语义改写。"
            "用户想看当下热议话题而不是查具体事件时（如「最近有什么大事」），"
            "provider 填 hotboard。"
            "涉及具体事件核查、纠纷责任或对事件的道德评价时，选择 web_search 并置 "
            "investigation_required=true；抽象伦理、创作和普通闲聊不启动事件调查。"
            "道德评价同时置 needs_moral_judgment=true，不预设谁错，不靠身份判责任。"
            "短追问应从 recent_investigation 或 recent_history 恢复事件检索词，"
            "不能拿‘现在呢’本身作query；旧摘要只是待查线索，不是本轮事实。"
            "你自己的模型记忆不是时效信息的来源，拿不准时倾向 web_search，"
            "不要凭记忆直接作答。"
            "无法判断时选择 clarify 或 casual_chat。JSON 示例："
            + json.dumps(schema, ensure_ascii=False)
        )},
        {"role": "user", "content": json.dumps({
            "message": message[:8000], "recent_history": context,
            "rule_hint": rule_hint.summary(), "recent_investigation": investigation_context or {},
            "group_social": ({
                "directed": bool(group_directed),
                "context_active": bool(group_context_active),
                "scene": group_scene or {},
            } if is_group else {}),
        }, ensure_ascii=False)},
    ]


async def plan_response(ctx: Any, runtime: Any, history: list[dict[str, Any]] | None = None) -> ResponsePlan:
    """LLM 自主选择响应模式；失败时回退到规则安全计划。"""
    prior = getattr(getattr(ctx, "trace", None), "investigation_context", {}) or {}
    is_group = bool(getattr(ctx, "is_group", False))
    group_directed = bool(getattr(ctx, "group_directed", True)) if is_group else False
    group_scene: dict[str, Any] = {}
    if is_group:
        try:
            from app.group import context as group_context

            group_scene = group_context.scene_summary(getattr(ctx, "group_id", ""))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            group_scene = {}
    hint = build_rule_plan(ctx.message, is_owner=ctx.is_owner, history=history, investigation_context=prior)
    if is_group:
        hint = apply_group_social_requirements(
            hint,
            message=ctx.message,
            history=history,
            addressed=group_directed,
            context_active=bool(getattr(ctx, "group_context_active", False)),
            enabled=getattr(runtime.settings, "group_social_enabled", True),
            interject_enabled=getattr(runtime.settings, "group_social_interject_enabled", False),
            min_confidence=getattr(runtime.settings, "group_social_min_confidence", 0.6),
            scene=group_scene,
        )
    if hint.intent == "current_datetime":
        hint.provider = "current_datetime"
        return hint
    if not getattr(runtime.settings, "semantic_planner_enabled", False):
        return hint
    model = str(getattr(runtime.settings, "response_plan_model", "") or "").strip() or runtime.settings.llm_model
    try:
        text = await runtime.llm.chat(
            build_planner_messages(
                ctx.message,
                history or [],
                hint,
                investigation_context=prior,
                is_group=is_group,
                group_directed=group_directed,
                group_context_active=bool(getattr(ctx, "group_context_active", False)),
                group_scene=group_scene,
            ),
            temperature=0.0,
            max_tokens=max(120, int(runtime.settings.response_plan_max_tokens)),
            response_format={"type": "json_object"},
            timeout=max(1.0, float(runtime.settings.response_plan_timeout)),
            model=model,
            request_id=ctx.request_id,
            user_id=ctx.uid,
            purpose="planner",
            # 规划失败可安全退回规则计划，不必重试（重试只会叠加首字延迟）
            retry_budget=0,
        )
        planned = parse_llm_plan(
            text,
            is_owner=ctx.is_owner,
            min_confidence=float(runtime.settings.response_plan_min_confidence),
            fallback=hint,
        )
        planned = apply_rule_requirements(planned, hint, is_owner=ctx.is_owner)
        if is_group:
            planned = apply_group_social_requirements(
                planned,
                message=ctx.message,
                history=history,
                addressed=group_directed,
                context_active=bool(getattr(ctx, "group_context_active", False)),
                enabled=getattr(runtime.settings, "group_social_enabled", True),
                interject_enabled=getattr(runtime.settings, "group_social_interject_enabled", False),
                min_confidence=getattr(runtime.settings, "group_social_min_confidence", 0.6),
                scene=group_scene,
            )
        return planned
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger("assistant.chat.response_plan").warning("响应 planner 失败，回退规则计划: %s", type(exc).__name__)
        return hint


def plan_datetime(message: str) -> dict[str, Any] | None:
    """返回北京时间的结构化当前日期时间事实。"""
    if not _TIME_RE.search(message or ""):
        return None
    current = now_local()
    weekday = "一二三四五六日"[current.weekday()]
    return {
        "date": current.strftime("%Y-%m-%d"),
        "month": current.month,
        "day": current.day,
        "weekday": f"星期{weekday}",
        "hour": current.hour,
        "minute": current.minute,
        "timezone": "Asia/Shanghai",
        "observed_at": current.isoformat(),
    }
