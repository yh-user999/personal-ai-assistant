"""统一响应策略：把消息分成事实、检索、推理、创作、情绪和行动模式。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.chat import values as values_module
from app.common.timeutil import now_local

_ALLOWED_PROVIDERS = frozenset({"current_datetime", "calculator", "web_search"})
_HIGH_RISK_WORDS = ("删除", "执行", "运行", "发送", "修改生产", "改配置")

# 无来源不判断：价值层的安全底线，必须先于任何道德/事实结论生效
NO_SOURCE_RULE = "本轮没有任何检索来源时必须回答「未查到」，不得凭模型记忆作答或补细节"

MODES = frozenset({
    "direct_fact", "retrieve_then_answer", "reasoning", "creative",
    "emotional_support", "action", "clarify", "refuse_or_confirm", "casual_chat",
    "moral_assessment",
})

# 道德评价模式的三条固定约束：事实与判断分离、必须表态、不越界定性
MORAL_CONSTRAINTS = (
    "按三层组织回答：已确认事实 / 说法不一致之处 / 我的判断",
    "核心是非必须明确表态，不用「各有各的道理」回避",
    "不得把传播量、情绪强度、措辞激烈程度当作依据",
    "不得对未确认事实作定性，不代替司法定罪或医学诊断",
)
SENSITIVE_CONSTRAINTS = (
    "涉及未成年人等敏感主体：不展开可识别身份细节",
    "不推测当事人动机，以官方通报为准",
)

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


def build_rule_plan(message: str, *, is_owner: bool = True) -> ResponsePlan:
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
    if values_module.looks_like_judgment_request(text):
        sensitive = values_module.is_sensitive_subject(text)
        constraints = list(MORAL_CONSTRAINTS)
        if sensitive:
            constraints.extend(SENSITIVE_CONSTRAINTS)
        return ResponsePlan(
            mode="moral_assessment", intent="moral_judgment", confidence=0.85,
            evidence_required=True, retrieval_required=True,
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


def parse_llm_plan(text: str, *, is_owner: bool = True, min_confidence: float = 0.60) -> ResponsePlan:
    """解析 LLM 策略 JSON；非法或低置信计划安全回退，不执行动作。"""
    import json

    try:
        raw = json.loads(text or "")
        if not isinstance(raw, dict):
            raise ValueError("plan is not object")
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
    except (json.JSONDecodeError, TypeError, ValueError):
        return ResponsePlan(mode="casual_chat", intent="planner_fallback", source="fallback")


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
        {"role": "system", "content": "你是私人助手的响应策略规划器。不要回答用户，只返回 JSON。自主判断用户意图和最佳响应模式，但不能授予权限、执行动作或编造事实。可选 mode: " + ", ".join(sorted(MODES)) + "。确定性时间/计算问题可选择 provider=current_datetime/calculator。无法判断时选择 clarify 或 casual_chat。JSON 示例：" + json.dumps(schema, ensure_ascii=False)},
        {"role": "user", "content": json.dumps({"message": message[:8000], "recent_history": context, "rule_hint": rule_hint.summary()}, ensure_ascii=False)},
    ]


async def plan_response(ctx: Any, runtime: Any, history: list[dict[str, Any]] | None = None) -> ResponsePlan:
    """LLM 自主选择响应模式；失败时回退到规则安全计划。"""
    hint = build_rule_plan(ctx.message, is_owner=ctx.is_owner)
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
        )
        return parse_llm_plan(text, is_owner=ctx.is_owner, min_confidence=float(runtime.settings.response_plan_min_confidence))
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
