"""群聊短时追问提示。

只生成有限枚举和时间预算，不保存原始消息、用户标识或隐藏推理。
"""
from __future__ import annotations

import re
from typing import Any

FOLLOWUP_KINDS = frozenset({"book_title", "yes_no", "choice", "free_text"})
DEFAULT_EXPIRES_IN = 90
DEFAULT_MAX_MESSAGES = 1

_BOOK_TITLE_RE = re.compile(r"哪本|书名|作品名|小说名|叫什么书|哪部小说")
_YES_NO_RE = re.compile(r"要不要|是否|是不是|能不能|可以吗|行不行|好不好|吗[？?。！!\s]*$")
_CHOICE_RE = re.compile(r"哪个|哪种|哪一个|选哪个|还是")
_FREE_TEXT_RE = re.compile(r"告诉我|说说|怎么想|什么感觉|怎么看|聊聊")
_QUESTION_RE = re.compile(r"[？?]|吗[。！!\s]*$|哪本|书名|要不要|哪个|怎么想")


def _plan_value(plan: Any, name: str, default: Any = None) -> Any:
    if isinstance(plan, dict):
        return plan.get(name, default)
    return getattr(plan, name, default)


def _kind_for_reply(reply: str) -> str:
    text = str(reply or "").strip()
    if _BOOK_TITLE_RE.search(text):
        return "book_title"
    if _YES_NO_RE.search(text):
        return "yes_no"
    if _CHOICE_RE.search(text):
        return "choice"
    if _FREE_TEXT_RE.search(text):
        return "free_text"
    return ""


def build_interaction_hint(ctx: Any, plan: Any, reply: str) -> dict[str, Any]:
    """为群聊澄清问题生成 QQ 端可消费的短时续接提示。"""
    if not bool(getattr(ctx, "is_group", False)):
        return {}
    text = str(reply or "").strip()
    if not text or not _QUESTION_RE.search(text):
        return {}

    kind = _kind_for_reply(text)
    if not kind:
        if bool(_plan_value(plan, "needs_clarification", False)):
            kind = "free_text"
        else:
            return {}

    # 直接回答型内容即使带问号，也不自动打开追问窗口；只接受明确澄清语境。
    action = str(_plan_value(plan, "social_action", "") or "").strip()
    needs_clarification = bool(_plan_value(plan, "needs_clarification", False))
    if not needs_clarification and action not in {"ask_back", "answer"}:
        return {}

    return {
        "followup": {
            "kind": kind if kind in FOLLOWUP_KINDS else "free_text",
            "expires_in": DEFAULT_EXPIRES_IN,
            "max_messages": DEFAULT_MAX_MESSAGES,
        }
    }
