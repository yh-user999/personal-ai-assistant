"""本地隐私过滤器：事件在离开本机前做敏感信息脱敏。

规则可配置（settings.privacy_filter），默认开启。
过滤字段：detail（窗口标题/页面标题/commit message）+ name（域名/窗口名——
浏览器地址栏里的公网 IP 也会被打码；内网/本机地址保持原样）。
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

RE_PUBLIC_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _mask_public_ip(m: re.Match) -> str:
    """公网 IP 打码（如 1.2.3.4 → 1.2.*.*）；内网/本机地址保持原样。"""
    parts = m.group(0).split(".")
    a = int(parts[0])
    b = int(parts[1]) if len(parts) > 1 else 0
    if a in (127, 10) or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168):
        return m.group(0)
    return f"{parts[0]}.{parts[1]}.*.*"


def sanitize(text: str) -> str:
    """对一段文本做脱敏。空文本原样返回。"""
    if not text:
        return text
    for pattern, repl in SENSITIVE_PATTERNS:
        text = re.sub(pattern, repl, text)
    text = RE_PUBLIC_IP.sub(_mask_public_ip, text)
    return text


def sanitize_event(event: dict) -> dict:
    """对事件的 detail 与 name 字段脱敏（就地修改并返回）。"""
    if "detail" in event:
        event["detail"] = sanitize(event["detail"])[:200]
    if "name" in event:
        event["name"] = sanitize(event["name"])[:200]
    return event
