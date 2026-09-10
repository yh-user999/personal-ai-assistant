"""统一响应策略：把消息分成事实、检索、推理、创作、情绪和行动模式。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.chat import values as values_module
from app.common.timeutil import now_local

_ALLOWED_PROVIDERS = frozenset({"current_datetime", "calculator", "web_search", "hotboard"})
_HIGH_RISK_WORDS = ("删除", "执行", "运行", "发送", "修改生产", "改配置")

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
    "核心是非必须明确表态，不用「各有各的道理」回避",
    "核心是非明确时，判断作主干立稳；对方的合理之处只作从属澄清"
    "（用「是…但不改变…」句式），不得与主判断等重并列、把立场对冲掉",
    "禁止用对称句式把有清晰是非的事拉平：不说「两边都有错/各打五十大板/"
    "一边…一边…」这类假平衡",
    "区分「摆平」与「摆对」：不得用「已调解/已赔付/程序走完」当道德是非的"
    "结论或背书；程序结果不代表事情就对了",
    "当受伤方、被指控方、弱势方是同一人时，不得用「对等纠纷」框架叙述，"
    "要点出事实上的不对等",
    "不得把传播量、情绪强度、措辞激烈程度当作依据",
    "不得对未确认事实作定性，不代替司法定罪或医学诊断",
    "立场强弱取决于事实充分度：核心事实有正规来源支撑才可硬表态，"
    "只有自媒体孤证的细节须标注来源并留余地",
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
    action: str | None = None
    risk: str = "low"
    needs_clarification: bool = False
    reason: str = ""
    needs_moral_judgment: bool = False
    sensitive_subject: bool = False
    stance_required: bool = True

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
            "action": self.action,
            "risk": self.risk,
            "tone": self.tone,
            "source": self.source,
            "query": self.query,
            "needs_moral_judgment": self.needs_moral_judgment,
            "sensitive_subject": self.sensitive_subject,
            "stance_required": self.stance_required,
            # constraints 必须带上：提示词靠它注入模式约束。
            # 漏掉会让道德/无来源/动作等约束全部静默失效。
            "constraints": list(self.constraints),
        }


def _previous_user_text(history: list[dict[str, Any]] | None) -> str:
    """历史里最近一条用户消息（本轮不在其中）。"""
    for item in reversed(history or []):
        if str(item.get("role") or "") != "user":
            continue
        text = str(item.get("content") or "").strip()
        if text:
            return text
    return ""


def build_rule_plan(
    message: str,
    *,
    is_owner: bool = True,
    history: list[dict[str, Any]] | None = None,
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
    from app.chat import web_provider

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
            provider="web_search", query=text[:400],
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
            provider="web_search", query=previous[:400],
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
            return ResponsePlan(mode="clarify" if confidence > 0.3 else "casual_chat", intent="uncertain", confidence=confidence, source="fallback", needs_clarification=confidence > 0.3)
        provider = str(raw.get("provider") or "").strip() or None
        if provider not in _ALLOWED_PROVIDERS:
            provider = None
        risk = str(raw.get("risk") or "low").strip().lower()
        if risk not in {"low", "medium", "high"}:
            risk = "low"
        action = str(raw.get("action") or "").strip()[:80] or None
        if any(word in (intent + " " + (action or "")) for word in _HIGH_RISK_WORDS):
            risk = "high"
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
            action=action,
            risk=risk,
            reason=str(raw.get("reason") or "").strip()[:120],
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
    if hint.provider == "web_search":
        plan.provider = "web_search"
        plan.tool_required = True
        plan.evidence_required = True
        plan.retrieval_required = True
        if plan.mode in {"casual_chat", "clarify"}:
            plan.mode = "retrieve_then_answer"
            plan.intent = hint.intent
        if not plan.query:
            plan.query = hint.query
        for item in hint.constraints:
            if item not in plan.constraints:
                plan.constraints.append(item)
    if hint.provider == "hotboard":
        # 热点浏览属规则强制项：planner 不认识 hotboard，会把 provider 清空，
        # 导致热榜不被调用（生产实测 intent=hot_browsing 但 provider='' ）。
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
    if plan.provider and plan.provider not in _ALLOWED_PROVIDERS:
        plan.provider = None
        plan.tool_required = False
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
    return plan


def build_planner_messages(message: str, history: list[dict[str, Any]], rule_hint: ResponsePlan) -> list[dict[str, str]]:
    import json

    schema = {
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
        "action": None,
        "risk": "low",
        "tone": "natural",
        "constraints": [],
    }
    context = [{"role": item.get("role", "user"), "content": str(item.get("content", ""))[:500]} for item in history[-4:]]
    return [
        {"role": "system", "content": (
            "你是私人助手的响应策略规划器。不要回答用户，只返回 JSON。"
            "自主判断用户意图和最佳响应模式，但不能授予权限、执行动作或编造事实。"
            "可选 mode: " + ", ".join(sorted(MODES)) + "。"
            "确定性时间/计算问题可选择 provider=current_datetime/calculator。"
            "只要问题涉及外部世界的时效信息——新闻、事件、公共人物/机构/地区/"
            "公司的近况与进展、政策或数据的当前值——provider 就填 web_search，"
            "query 填用户原话（原话比改写命中更多）。"
            "用户想看当下热议话题而不是查具体事件时（如「最近有什么大事」），"
            "provider 填 hotboard。"
            "你自己的模型记忆不是时效信息的来源，拿不准时倾向 web_search，"
            "不要凭记忆直接作答。"
            "无法判断时选择 clarify 或 casual_chat。JSON 示例："
            + json.dumps(schema, ensure_ascii=False)
        )},
        {"role": "user", "content": json.dumps({"message": message[:8000], "recent_history": context, "rule_hint": rule_hint.summary()}, ensure_ascii=False)},
    ]


async def plan_response(ctx: Any, runtime: Any, history: list[dict[str, Any]] | None = None) -> ResponsePlan:
    """LLM 自主选择响应模式；失败时回退到规则安全计划。"""
    hint = build_rule_plan(ctx.message, is_owner=ctx.is_owner, history=history)
    if hint.intent == "current_datetime":
        hint.provider = "current_datetime"
        return hint
    if not getattr(runtime.settings, "semantic_planner_enabled", False):
        return hint
    model = str(getattr(runtime.settings, "response_plan_model", "") or "").strip() or runtime.settings.llm_model
    try:
        text = await runtime.llm.chat(
            build_planner_messages(ctx.message, history or [], hint),
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
        return apply_rule_requirements(planned, hint, is_owner=ctx.is_owner)
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
