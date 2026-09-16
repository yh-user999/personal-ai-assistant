"""OneBot 网关配置：只从环境变量读取，不把密钥写入代码或日志。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from os import environ
from urllib.parse import urlparse


_TRUE = frozenset({"1", "true", "yes", "on", "y"})
_FALSE = frozenset({"0", "false", "no", "off", "n"})
_GROUP_SPLIT_RE = re.compile(r"[\s,;]+")


def _env(name: str, default: str = "") -> str:
    return str(environ.get(name, default) or "").strip()


def _bool(name: str, default: bool) -> bool:
    value = _env(name)
    if not value:
        return default
    normalized = value.casefold()
    if normalized in _TRUE:
        return True
    if normalized in _FALSE:
        return False
    return default


def _int(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    value = _env(name)
    try:
        result = int(value) if value else default
    except ValueError:
        result = default
    result = max(minimum, result)
    return min(maximum, result) if maximum is not None else result


def _float(name: str, default: float, *, minimum: float = 0.0) -> float:
    value = _env(name)
    try:
        result = float(value) if value else default
    except ValueError:
        result = default
    return max(minimum, result)


def parse_group_allowlist(value: str) -> tuple[bool, frozenset[str]]:
    """解析群白名单；空值不允许任何群，单独的 ``*`` 才表示全开。"""
    tokens = [item for item in _GROUP_SPLIT_RE.split(str(value or "").strip()) if item]
    allow_all = "*" in tokens
    groups = frozenset(item for item in tokens if item != "*" and item.isdigit() and len(item) <= 32)
    return allow_all, groups


@dataclass(frozen=True, slots=True)
class GatewaySettings:
    """网关运行时配置；敏感字段不参与 dataclass repr。"""

    listen_host: str = "127.0.0.1"
    listen_port: int = 3101
    server_api_base: str = "http://127.0.0.1:8000"
    onebot_api_url: str = field(default="", repr=False)
    onebot_token: str = field(default="", repr=False)
    inbound_token: str = field(default="", repr=False)
    qq_api_token: str = field(default="", repr=False)
    identity_secret: str = field(default="", repr=False)
    owner_api_token: str = field(default="", repr=False)
    owner_id: str = field(default="", repr=False)
    group_allow_all: bool = False
    group_allowed_ids: frozenset[str] = frozenset()
    configured_self_id: str = ""
    group_require_mention: bool = True
    group_trigger_prefix: str = ""
    group_interject_enabled: bool = False
    group_cooldown_seconds: float = 0.0
    group_max_replies_per_hour: int = 30
    followup_enabled: bool = True
    followup_window_seconds: float = 90.0
    followup_max_messages: int = 1
    request_timeout_seconds: float = 120.0
    max_message_chars: int = 2000
    max_reply_chars: int = 4000
    send_error_reply: bool = True

    def validate(self) -> None:
        """启动期校验关键配置；缺少密钥时宁可网关不起也不降级放行。"""
        parsed = urlparse(self.server_api_base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("QQ_GATEWAY_API_BASE 必须是 http(s) URL")
        onebot = urlparse(self.onebot_api_url)
        if onebot.scheme not in {"http", "https"} or not onebot.netloc:
            raise ValueError("QQ_GATEWAY_ONEBOT_URL/QQ_PUSH_URL 必须是 http(s) URL")
        if not self.qq_api_token:
            raise ValueError("QQ_API_TOKEN 未配置")
        if not self.identity_secret:
            raise ValueError("QQ_IDENTITY_SECRET 未配置")
        if not self._is_loopback_host() and not self.inbound_token:
            raise ValueError("非回环监听必须配置 QQ_GATEWAY_INBOUND_TOKEN")

    def group_allowed(self, group_id: str) -> bool:
        group = str(group_id or "").strip()
        return bool(group and (self.group_allow_all or group in self.group_allowed_ids))

    def _is_loopback_host(self) -> bool:
        return self.listen_host in {"127.0.0.1", "localhost", "::1"}

    @classmethod
    def from_env(cls) -> GatewaySettings:
        allow_all, allowed_ids = parse_group_allowlist(_env("QQ_GATEWAY_GROUP_ALLOWED_IDS"))
        owner_api_token = _env("OWNER_API_TOKEN") or _env("API_TOKEN")
        return cls(
            listen_host=_env("QQ_GATEWAY_HOST", "127.0.0.1"),
            listen_port=_int("QQ_GATEWAY_PORT", 3101, minimum=1, maximum=65535),
            server_api_base=_env("QQ_GATEWAY_API_BASE", "http://127.0.0.1:8000").rstrip("/"),
            # 优先网关专用出口；未配置时回退服务端提醒推送使用的 QQ_PUSH_*。
            onebot_api_url=(_env("QQ_GATEWAY_ONEBOT_URL") or _env("QQ_PUSH_URL")).rstrip("/"),
            onebot_token=_env("QQ_GATEWAY_ONEBOT_TOKEN") or _env("QQ_PUSH_TOKEN"),
            inbound_token=_env("QQ_GATEWAY_INBOUND_TOKEN"),
            qq_api_token=_env("QQ_API_TOKEN"),
            identity_secret=_env("QQ_IDENTITY_SECRET"),
            owner_api_token=owner_api_token,
            owner_id=_env("QQ_GATEWAY_OWNER_ID") or _env("QQ_ADMIN_ID"),
            group_allow_all=allow_all,
            group_allowed_ids=allowed_ids,
            configured_self_id=_env("QQ_GATEWAY_SELF_ID"),
            group_require_mention=_bool("QQ_GATEWAY_GROUP_REQUIRE_MENTION", True),
            group_trigger_prefix=_env("QQ_GATEWAY_GROUP_TRIGGER_PREFIX"),
            group_interject_enabled=_bool("QQ_GATEWAY_GROUP_INTERJECT_ENABLED", False),
            group_cooldown_seconds=_float("QQ_GATEWAY_GROUP_COOLDOWN_SECONDS", 0.0),
            group_max_replies_per_hour=_int(
                "QQ_GATEWAY_GROUP_MAX_REPLIES_PER_HOUR", 30, minimum=1, maximum=10000
            ),
            followup_enabled=_bool("QQ_GATEWAY_FOLLOWUP_ENABLED", True),
            followup_window_seconds=_float("QQ_GATEWAY_FOLLOWUP_WINDOW_SECONDS", 90.0, minimum=1.0),
            followup_max_messages=_int("QQ_GATEWAY_FOLLOWUP_MAX_MESSAGES", 1, minimum=1, maximum=10),
            request_timeout_seconds=_float("QQ_GATEWAY_REQUEST_TIMEOUT_SECONDS", 120.0, minimum=1.0),
            max_message_chars=_int("QQ_GATEWAY_MAX_MESSAGE_CHARS", 2000, minimum=1, maximum=20000),
            max_reply_chars=_int("QQ_GATEWAY_MAX_REPLY_CHARS", 4000, minimum=1, maximum=20000),
            send_error_reply=_bool("QQ_GATEWAY_SEND_ERROR_REPLY", True),
        )


settings = GatewaySettings.from_env()

__all__ = ["GatewaySettings", "parse_group_allowlist", "settings"]
