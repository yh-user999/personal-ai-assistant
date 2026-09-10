"""统一响应策略：把消息分成事实、检索、推理、创作、情绪和行动模式。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.common.timeutil import now_local

MODES = frozenset({
    "direct_fact", "retrieve_then_answer", "reasoning", "creative",
    "emotional_support", "action", "clarify", "refuse_or_confirm", "casual_chat",
})

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

    def summary(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "intent": self.intent,
            "confidence": round(max(0.0, min(1.0, self.confidence)), 3),
            "evidence_required": self.evidence_required,
            "retrieval_required": self.retrieval_required,
            "tool_required": self.tool_required,
            "confirmation_required": self.confirmation_required,
            "tone": self.tone,
            "source": self.source,
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
