"""脱敏规则单一来源：采集器（出网前）与服务端（入库前）共用同一份判据。

为什么要合并（审计发现）：
两端各自维护一份正则，规则已经漂移——采集器漏掉了中文键名、全角冒号、
GitHub/AWS/JWT 三类 token，服务端则漏掉了中文键名。实测：
  '密码：hunter2'  采集器未脱敏 / 服务端未脱敏
  'ghp_xxx'        采集器未脱敏
  '手机13812345678' 采集器未脱敏（\\b 在中文与数字间不成立）
窗口标题里出现「密码：xxx」是常见场景，这些原文会上传服务器并进 LLM 上下文。

设计要点：
- 中文与 ASCII 混排时 \\b 不可靠（中文属于 \\w，"手机13812345678" 里
  中文与数字之间没有词边界）。数字类改用 (?<!\\d) / (?!\\d) 显式排除数字邻接。
- 键值类（密码/token）覆盖中英键名与半角/全角分隔符。
- 顺序有讲究：长模式在前（身份证 18 位含手机号子串；JWT 含 base64 段）。
"""
import re

# ── 键值型敏感信息（密码/口令/密钥/token）──────────────────
# 键名：中英双语；分隔符：= : ：（全角）以及空格。值取到空白或引号为止。
_KEY_NAMES = (
    r"password|passwd|pwd|secret|token|api[_\-]?key|access[_\-]?key|"
    r"私钥|密钥|密码|口令|凭证|令牌"
)
RE_KEYVALUE = re.compile(
    rf"(?i)(?P<key>{_KEY_NAMES})\s*(?P<sep>[=:：]|\s)\s*(?P<val>[^\s'\"，,;；]{{2,}})"
)

# ── 具名凭证格式（有固定前缀，可精确识别）──────────────────
RE_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}")
RE_GITHUB_TOKEN = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}")
RE_AWS_KEY = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
RE_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}")
RE_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

# ── 个人信息 ───────────────────────────────────────────────
# 不用 \b：中文相邻时词边界不成立。用 (?<!\d)/(?!\d) 只排除数字邻接，
# 这样"手机13812345678"能命中，而 18 位订单号里的子串不会被误判为手机号。
RE_IDCARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
RE_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
RE_BANKCARD = re.compile(r"(?<!\d)4\d{12}(?:\d{3})?(?!\d)")
# IP：四段各自 0-255 才算（旧正则会把"版本 1.2.3.4"打成 1.2.*.*）
_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)"
RE_IP = re.compile(rf"(?<![\d.]){_OCTET}(?:\.{_OCTET}){{3}}(?![\d.])")


def _mask_keyvalue(m: re.Match) -> str:
    sep = m.group("sep")
    sep = sep if sep.strip() else "="
    return f"{m.group('key')}{sep}[REDACTED]"


def _mask_phone(m: re.Match) -> str:
    s = m.group(0)
    return s[:3] + "****" + s[-4:]


def _mask_email(m: re.Match) -> str:
    local, domain = m.group(0).split("@", 1)
    return local[:3] + "***@" + domain


def _mask_idcard(m: re.Match) -> str:
    s = m.group(0)
    return s[:6] + "********" + s[-4:]


def is_private_ip(s: str) -> bool:
    """内网/本机地址不算敏感（技术文档里的 curl 127.0.0.1 不该被打码）。"""
    parts = s.split(".")
    try:
        a, b = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return False
    return (
        a in (0, 127, 10)
        or (a == 172 and 16 <= b <= 31)
        or (a == 192 and b == 168)
        or (a == 169 and b == 254)
    )


# 版本号语境：'版本 1.2.3.4' / 'v1.2.3.4' / 'Python 3.4.5.6' 形态与 IP 完全
# 相同，只能靠上下文区分。命中这些前导词时不打码——技术对话里版本号远比
# 公网 IP 常见，误打码会让"升级到 1.2.3.4"变成"升级到 1.2.*.*"。
RE_VERSION_CONTEXT = re.compile(
    r"(?i)(版本|版本号|升级到|更新到|ver\.?|version|v)\s*$"
)


def _mask_ip(m: re.Match) -> str:
    s = m.group(0)
    if is_private_ip(s):
        return s
    # 看前文是否是版本号语境
    prefix = m.string[max(0, m.start() - 12):m.start()]
    if RE_VERSION_CONTEXT.search(prefix):
        return s
    parts = s.split(".")
    return f"{parts[0]}.{parts[1]}.*.*"


# 处理顺序：长/具名模式在前。身份证 18 位内含手机号模式；JWT 的段
# 可能被邮箱或 keyvalue 部分吞掉；私钥头部要在通用规则前替换。
_STEPS: list[tuple[re.Pattern, object]] = [
    (RE_PRIVATE_KEY, "[PRIVATE_KEY]"),
    (RE_JWT, "[JWT]"),
    (RE_GITHUB_TOKEN, "[GITHUB_TOKEN]"),
    (RE_AWS_KEY, "[AWS_KEY]"),
    (RE_OPENAI_KEY, "[API_KEY]"),
    (RE_KEYVALUE, _mask_keyvalue),
    (RE_IDCARD, _mask_idcard),
    (RE_BANKCARD, "[CARD]"),
    (RE_PHONE, _mask_phone),
    (RE_EMAIL, _mask_email),
    (RE_IP, _mask_ip),
]


def redact(text: str) -> str:
    """按统一规则脱敏一段文本。空文本原样返回。"""
    if not text:
        return text
    for pattern, repl in _STEPS:
        text = pattern.sub(repl, text)
    return text
