"""群聊短时追问提示。

只生成有限枚举和时间预算，不保存原始消息、用户标识或隐藏推理。
"""
from __future__ import annotations

import re
from typing import Any

FOLLOWUP_KINDS = frozenset({"book_title", "yes_no", "choice", "free_text"})
DEFAULT_EXPIRES_IN = 90
DEFAULT_MAX_MESSAGES = 1
CONTEXT_WINDOW_EXPIRES_IN = 90
CONTEXT_WINDOW_MAX_MESSAGES = 10
CONTEXT_WINDOW_MAX_NONCONTINUATIONS = 5

_BOOK_TITLE_RE = re.compile(r"哪本|书名|作品名|小说名|叫什么书|哪部小说")
_BOOK_CONTEXT_RE = re.compile(r"小说|书|作者|作品")
_MATERIAL_REQUEST_RE = re.compile(
    r"(?:发|贴|给|提供|附上).{0,12}(?:链接|简介|资料|书名|作者|作品)|"
    r"(?:链接|简介|资料|书名|作者|作品).{0,12}(?:发|贴|给|提供)"
)
_YES_NO_RE = re.compile(r"要不要|是否|是不是|能不能|可以吗|行不行|好不好|吗[？?。！!\s]*$")
_CHOICE_RE = re.compile(r"哪个|哪种|哪一个|选哪个|还是")
_FREE_TEXT_RE = re.compile(r"告诉我|说说|怎么想|什么感觉|怎么看|聊聊")
_QUESTION_RE = re.compile(
    r"[？?]|吗[。！!\s]*$|哪本|书名|要不要|哪个|怎么想"
)


def _plan_value(plan: Any, name: str, default: Any = None) -> Any:
    if isinstance(plan, dict):
        return plan.get(name, default)
    return getattr(plan, name, default)


def _kind_for_reply(reply: str, original_message: str = "") -> str:
    text = str(reply or "").strip()
    origin = str(original_message or "").strip()
    if _BOOK_TITLE_RE.search(text) or (
        _BOOK_CONTEXT_RE.search(origin) and _MATERIAL_REQUEST_RE.search(text)
    ):
        return "book_title"
    if _MATERIAL_REQUEST_RE.search(text):
        return "free_text"
    if _YES_NO_RE.search(text):
        return "yes_no"
    if _CHOICE_RE.search(text):
        return "choice"
    if _FREE_TEXT_RE.search(text):
        return "free_text"
    return ""


def build_context_window_hint(ctx: Any, plan: Any, reply: str = "") -> dict[str, Any]:
    """构造 @ 后上下文窗口元数据；不保存原文、用户标识或隐藏推理。"""
    if not bool(getattr(ctx, "is_group", False)):
        return {}
    directed = bool(getattr(ctx, "group_directed", False))
    active = bool(getattr(ctx, "group_context_active", False))
    if not directed and not active:
        return {}
    if not active and not str(reply or "").strip():
        return {}
    relation = str(_plan_value(plan, "context_relation", "new_topic") or "new_topic").strip()
    if relation not in {"new_topic", "continues_previous", "unclear"}:
        relation = "unclear"
    reply_decision = str(_plan_value(plan, "reply_decision", "answer") or "answer").strip()
    if reply_decision not in {"ignore", "answer", "ask_back", "interject"}:
        reply_decision = "ignore"
    return {
        "context_window": {
            "open": True,
            "expires_in": CONTEXT_WINDOW_EXPIRES_IN,
            "max_messages": CONTEXT_WINDOW_MAX_MESSAGES,
            "max_noncontinuations": CONTEXT_WINDOW_MAX_NONCONTINUATIONS,
            "context_relation": relation,
            "context_continue": bool(_plan_value(plan, "context_continue", False)),
            "context_close_reason": str(_plan_value(plan, "context_close_reason", "") or "")[:40],
            "reply_decision": reply_decision,
            "analysis_only": bool(_plan_value(plan, "analysis_only", False)),
        }
    }


def build_interaction_hint(ctx: Any, plan: Any, reply: str) -> dict[str, Any]:
    """生成群聊的窗口提示，并兼容旧的单次 follow-up 契约。"""
    interaction = build_context_window_hint(ctx, plan, reply)
    # @ 后窗口优先于旧的单次 follow-up，避免两个状态机叠加导致窗口超额。
    if "context_window" in interaction:
        return interaction
    if not bool(getattr(ctx, "is_group", False)):
        return interaction
    text = str(reply or "").strip()
    if not text:
        return interaction
    planned_required = bool(_plan_value(plan, "followup_required", False))
    if not planned_required and not (_QUESTION_RE.search(text) or _MATERIAL_REQUEST_RE.search(text)):
        return interaction

    planned_kind = str(_plan_value(plan, "followup_kind", "") or "").strip()
    kind = planned_kind if planned_required and planned_kind in FOLLOWUP_KINDS else _kind_for_reply(
        text, getattr(ctx, "message", "")
    )
    if not kind:
        if bool(_plan_value(plan, "needs_clarification", False)) or planned_required:
            kind = "free_text"
        else:
            return interaction

    action = str(_plan_value(plan, "social_action", "") or "").strip()
    needs_clarification = bool(_plan_value(plan, "needs_clarification", False))
    if not planned_required and not needs_clarification and action not in {"ask_back", "answer"}:
        return interaction

    interaction["followup"] = {
        "kind": kind if kind in FOLLOWUP_KINDS else "free_text",
        "expires_in": DEFAULT_EXPIRES_IN,
        "max_messages": DEFAULT_MAX_MESSAGES,
    }
    return interaction
