"""本地隐私过滤器：事件在离开本机前做敏感信息脱敏。

规则可配置（settings.privacy_filter），默认开启。
注意：只脱敏 detail 字段（窗口标题/页面标题/commit message），
域名/应用名等聚合维度不过滤（统计需要）。
"""
import re

SENSITIVE_PATTERNS: list[tuple[str, str]] = [
    # (正则, 替换串)
    (r"(?i)(password|passwd|pwd)\s*[=:]\s*\S+", r"\1=[REDACTED]"),
    (r"(?i)(token|api[_-]?key|secret)\s*[=:]\s*[\w\-\.]+", r"\1=[REDACTED]"),
    (r"\b4[0-9]{12}(?:[0-9]{3})?\b", "[CARD]"),          # 银行卡号（Luhn 未验，粗筛）
    (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[EMAIL]"),
    (r"\bsk-[A-Za-z0-9_\-]{8,}\b", "[API_KEY]"),          # OpenAI 风格密钥
    (r"\b1[3-9]\d{9}\b", "[PHONE]"),                      # 大陆手机号
]


def sanitize(text: str) -> str:
    """对一段文本做脱敏。空文本原样返回。"""
    if not text:
        return text
    for pattern, repl in SENSITIVE_PATTERNS:
        text = re.sub(pattern, repl, text)
    return text


def sanitize_event(event: dict) -> dict:
    """对事件的 detail 字段脱敏（就地修改并返回）。"""
    if "detail" in event:
        event["detail"] = sanitize(event["detail"])[:200]
    return event
