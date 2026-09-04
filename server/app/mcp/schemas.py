"""MCP 输入校验、结果预算与安全摘要。"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from app.config import settings


class McpInputError(ToolError):
    """调用参数不符合 MCP 第一阶段边界。"""


def bounded_text(value: str, *, name: str, max_chars: int | None = None) -> str:
    text = str(value or "").strip()
    limit = max_chars or settings.mcp_max_input_chars
    if not text:
        raise McpInputError(f"{name} 不能为空")
    if len(text) > limit:
        raise McpInputError(f"{name} 不能超过 {limit} 个字符")
    return text


def bounded_limit(value: int, *, name: str, default: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise McpInputError(f"{name} 必须是整数") from exc
    if number < 1:
        return default
    return min(number, maximum)


def _safe_value(value: Any, *, string_limit: int = 120) -> Any:
    if isinstance(value, str):
        return value[:string_limit] + ("…" if len(value) > string_limit else "")
    if isinstance(value, Mapping):
        return {str(k): _safe_value(v, string_limit=string_limit) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [_safe_value(v, string_limit=string_limit) for v in value[:50]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:string_limit]


def summarize_arguments(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    """生成不含秘密全文的审计摘要。"""
    sensitive = {"content", "query", "text", "title", "note", "token", "password", "secret"}
    result: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        name = str(key)
        if isinstance(value, str) and name.casefold() in sensitive:
            result[name] = {"length": len(value)}
        else:
            result[name] = _safe_value(value)
    return result


def _trim(value: Any, budget: int) -> Any:
    """递归裁剪为 JSON 可序列化对象，尽量保留顶层结构。"""
    if budget <= 0:
        return "…"
    if isinstance(value, str):
        return value if len(value) <= budget else value[: max(1, budget - 1)] + "…"
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            out[str(key)] = _trim(item, max(32, budget // max(1, len(value))))
        return out
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        out = []
        for item in value:
            out.append(_trim(item, max(32, budget // max(1, len(value) or 1))))
        return out
    return _safe_value(value, string_limit=max(32, budget))


def cap_payload(payload: Any, max_chars: int | None = None) -> Any:
    """限制 MCP 返回的 JSON 字符数，避免长记忆/知识块撑爆 Host。"""
    budget = max(64, int(max_chars or settings.mcp_max_result_chars))
    candidate = _safe_value(payload, string_limit=min(4000, budget))
    encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(encoded) <= budget:
        return candidate

    # 优先丢弃列表尾部，保留排名靠前的结果。
    if isinstance(candidate, dict):
        for key, value in list(candidate.items()):
            if isinstance(value, list):
                while value and len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))) > budget:
                    value.pop()
                candidate[key] = value
        candidate["truncated"] = True
        encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(encoded) <= budget:
            return candidate

    # 极端长字符串/深层结构退化为合法的短预览，并严格保证预算。
    marker = {"truncated": True, "preview": ""}
    source = encoded
    low, high = 0, len(source)
    while low < high:
        mid = (low + high + 1) // 2
        marker["preview"] = source[:mid]
        if len(json.dumps(marker, ensure_ascii=False, separators=(",", ":"))) <= budget:
            low = mid
        else:
            high = mid - 1
    marker["preview"] = source[:low]
    return marker


def error_payload(message: str, *, code: str = "mcp_error") -> dict[str, str]:
    return {"error": code, "message": message}
