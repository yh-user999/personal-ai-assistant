"""从 LLM 输出里稳健地取出 JSON 对象。

实测教训：模型常在 JSON 前后带解释文字、思考段或 ```json 围栏，直接
``json.loads(整体输出)`` 会失败。更糟的是失败后如果静默回退成默认值，
调用方会以为"模型没意见"，而真实原因是格式没解析出来——生产上表现为
"planner 明明可用却从没生效过"。
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("assistant.llm_json")

# 单次日志里回显的原文长度上限（模型输出，不含用户隐私）
RAW_LOG_LIMIT = 300


def extract_json_object(text: str) -> dict | None:
    """从任意文本里取出第一个完整 JSON 对象；取不到返回 None。

    步骤：去 ``` 围栏 → 直接尝试 → 退化为首个 '{' 到最末 '}' 的子串。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        lines = [line for line in raw.splitlines() if not line.strip().startswith("```")]
        raw = "\n".join(lines).strip()
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        value = json.loads(raw[start : end + 1])
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def log_unparsed(label: str, text: str) -> None:
    """解析失败时留证据：截断回显原文，便于判断是格式问题还是被截断。"""
    raw = (text or "").strip().replace("\n", " ")
    logger.warning(
        "%s 输出无法解析为 JSON（%d 字符，尾部：...%s）",
        label,
        len(text or ""),
        raw[-RAW_LOG_LIMIT:],
    )
