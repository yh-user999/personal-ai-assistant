"""脱敏核心（第 6.14 课）：所有入库文本统一先过这里再落库。

原则："服务器不该存用户的明文敏感信息"——
- 自动识别：手机号（131****2351）、邮箱（wfy***@163.com）、
  公网 IP（101.33.*.*）、身份证号（前6后4保留）
- 自定义词：.env SENSITIVE_TERMS（姓名/公司/学校等，分号分隔）→ 替换为"已脱敏"
- 内网/本机地址（127.x、10.x、172.16-31.x、192.168.x）不算敏感，保持原样
  （技术文档里的 curl 127.0.0.1 示例不能被打码）

接入点：记忆写入 / 知识库入库 / 文档保存 / 行为事件入库。
"""
import re

from app.config import settings

RE_PHONE = re.compile(r"1[3-9]\d{9}")
RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
RE_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
RE_IDCARD = re.compile(r"\b\d{17}[\dXx]\b")


def _mask_phone(m: re.Match) -> str:
    s = m.group(0)
    return s[:3] + "****" + s[-4:]


def _mask_email(m: re.Match) -> str:
    local, domain = m.group(0).split("@", 1)
    return local[:3] + "***@" + domain


def _is_private_ip(s: str) -> bool:
    parts = s.split(".")
    a = int(parts[0])
    b = int(parts[1]) if len(parts) > 1 else 0
    return a in (127, 10) or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)


def _mask_ip(m: re.Match) -> str:
    s = m.group(0)
    if _is_private_ip(s):
        return s
    parts = s.split(".")
    return f"{parts[0]}.{parts[1]}.*.*"


def _mask_idcard(m: re.Match) -> str:
    s = m.group(0)
    return s[:6] + "********" + s[-4:]


def _custom_terms() -> list[str]:
    return [
        t.strip()
        for t in settings.sensitive_terms.replace(",", ";").split(";")
        if t.strip()
    ]


def sanitize(text: str) -> str:
    """对文本做统一脱敏。不可逆，原值只在用户本地保存。

    顺序：身份证在前（18 位数字里可能嵌着 11 位手机号模式的子串，必须先处理）。
    """
    if not text:
        return text
    t = RE_IDCARD.sub(_mask_idcard, text)
    t = RE_PHONE.sub(_mask_phone, t)
    t = RE_EMAIL.sub(_mask_email, t)
    t = RE_IP.sub(_mask_ip, t)
    for term in _custom_terms():
        t = t.replace(term, "已脱敏")
    return t
