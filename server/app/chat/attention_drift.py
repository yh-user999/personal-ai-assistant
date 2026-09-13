"""群聊注意力漂移与自然短反应规则。

只提供行为边界，不制造事实，不复制外部项目提示词。默认轻微漂移、严格回钩、克制反应。
"""
from __future__ import annotations

from typing import Any

_DRIFT_RULES = {
    "subtle": "只在最近消息有非常自然的触发点时轻轻联想一句，大多数时候继续当前话题。",
    "active": "可以抓住新鲜、好笑、反差或熟悉的细节接一句，但仍要短，并回到当前话题。",
}
_ANCHOR_RULES = {
    "strict": "短暂联想后立刻回到当前问题或被回复对象，不要无故换题。",
    "balanced": "可以沿支线说一句，但主要意思或结尾通常要回到当前聊天。",
}
_REACTION_RULES = {
    "reserved": "只有特别适合时才用一句很短的反应开头，不要每轮都加语气词。",
    "natural": "可以偶尔先用短句、吐槽或语气词接住话题，再继续正常回答。",
}


def _choice(settings: Any, name: str, allowed: set[str], default: str) -> str:
    value = str(getattr(settings, name, default) or "").strip().casefold()
    return value if value in allowed else default


def normalize_settings(settings: Any) -> dict[str, str]:
    return {
        "drift_level": _choice(settings, "group_drift_level", set(_DRIFT_RULES), "subtle"),
        "anchor_policy": _choice(settings, "group_anchor_policy", set(_ANCHOR_RULES), "strict"),
        "reaction_style": _choice(settings, "group_reaction_style", set(_REACTION_RULES), "reserved"),
    }


def build_prompt_block(settings: Any) -> str:
    """生成自然表达规则；不启用时返回空字符串。"""
    if not bool(getattr(settings, "group_heartflow_enabled", True)):
        return ""
    values = normalize_settings(settings)
    return (
        "【群聊自然表达】\n"
        "你可以像一个有连续注意力的群成员一样接话，但不要为了拟人而故意低效。"
        "每次明显拐弯最多一次，且必须能从最近消息找到触发点。"
        "不要自称分心，不要使用医学化标签，不要凭空补充事实。"
        f"漂移档位：{_DRIFT_RULES[values['drift_level']]}"
        f"回钩策略：{_ANCHOR_RULES[values['anchor_policy']]}"
        f"短反应：{_REACTION_RULES[values['reaction_style']]}"
        "明确问题必须先回答，不能用闲聊或玩笑逃避。"
    )
