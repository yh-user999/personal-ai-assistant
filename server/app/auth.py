"""统一认证上下文与角色权限。"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.config import settings


@dataclass(frozen=True)
class AuthContext:
    token: str
    role: str
    # owner/internal 固定为主人；qq 在中间件完成签名校验后写入 QQ 号。
    subject: str | None = None


ROLE_TOKENS = {
    "owner": "owner_api_token",
    "internal": "internal_api_token",
    "collector": "collector_api_token",
    "executor": "executor_api_token",
    "qq": "qq_api_token",
}

QQ_USER_HEADER = "x-qq-user-id"
QQ_TIMESTAMP_HEADER = "x-qq-timestamp"
QQ_SIGNATURE_HEADER = "x-qq-signature"
QQ_REQUEST_ID_HEADER = "x-qq-request-id"
QQ_FALLBACK_REQUEST_ID_HEADER = "x-request-id"
DEFAULT_QQ_IDENTITY_MAX_AGE_SECONDS = 300


def _safe_eq(a: str, b: str) -> bool:
    """compare_digest 的字节版封装：str 版对非 ASCII 直接抛 TypeError，
    曾让任意未认证请求用一个中文 token 就打成 500。"""
    return secrets.compare_digest(a.encode(), b.encode())


def _owner_subject() -> str:
    # 延迟导入，避免认证模块与记忆模块互相导入时形成循环。
    from app.core.memory import owner_user_id

    return owner_user_id()


def _subject_for_role(role: str) -> str | None:
    if role in {"owner", "internal"}:
        return _owner_subject()
    return None


def qq_identity_payload(user_id: str, timestamp: str | int, request_id: str) -> bytes:
    """QQ 身份签名的规范化载荷。

    三个字段逐行拼接，避免分隔符歧义；插件与服务端共用该格式。
    """
    return f"{str(user_id).strip()}\n{str(timestamp).strip()}\n{str(request_id).strip()}".encode()


def sign_qq_identity(secret: str, user_id: str, timestamp: str | int, request_id: str) -> str:
    """生成 QQ 身份 HMAC-SHA256 十六进制签名。"""
    return hmac.new(
        str(secret).encode(),
        qq_identity_payload(user_id, timestamp, request_id),
        hashlib.sha256,
    ).hexdigest()


def _qq_identity_error(detail: str, *, status_code: int = 401) -> HTTPException:
    # 不区分字段是否存在和签名是否匹配，避免给攻击者提供签名 oracle。
    return HTTPException(status_code=status_code, detail=detail)


def verify_qq_identity(request: Request) -> tuple[str, str]:
    """校验 QQ 侧传来的用户身份签名，返回 ``(user_id, request_id)``。

    身份密钥缺失、字段缺失、时间窗口外或签名不匹配均 fail-closed。
    """
    secret = str(getattr(settings, "qq_identity_secret", "") or "").strip()
    if not secret:
        # 共享身份密钥缺失是权限配置错误，保留 403 语义兼容旧客户端；
        # 密钥已配置但请求不完整时仍返回 401。
        raise _qq_identity_error("qq identity signature required", status_code=403)

    user_id = request.headers.get(QQ_USER_HEADER, "").strip()
    timestamp = request.headers.get(QQ_TIMESTAMP_HEADER, "").strip()
    signature = request.headers.get(QQ_SIGNATURE_HEADER, "").strip().lower()
    request_id = (
        request.headers.get(QQ_REQUEST_ID_HEADER, "").strip()
        or request.headers.get(QQ_FALLBACK_REQUEST_ID_HEADER, "").strip()
    )
    if not user_id or not timestamp or not signature or not request_id:
        raise _qq_identity_error("invalid qq identity signature")
    if len(user_id) > 32 or len(request_id) > 160 or len(signature) > 128:
        raise _qq_identity_error("invalid qq identity signature")
    if not user_id.isdigit():
        raise _qq_identity_error("invalid qq identity signature")
    try:
        timestamp_value = float(timestamp)
    except (TypeError, ValueError) as exc:
        raise _qq_identity_error("invalid qq identity signature") from exc
    max_age = getattr(settings, "qq_identity_max_age_seconds", DEFAULT_QQ_IDENTITY_MAX_AGE_SECONDS)
    try:
        max_age = max(1.0, float(max_age))
    except (TypeError, ValueError):
        max_age = DEFAULT_QQ_IDENTITY_MAX_AGE_SECONDS
    if abs(time.time() - timestamp_value) > max_age:
        raise _qq_identity_error("expired qq identity signature")

    expected = sign_qq_identity(secret, user_id, timestamp, request_id)
    if not _safe_eq(signature, expected):
        # 只返回统一错误，避免暴露哪一个字段被篡改。
        raise _qq_identity_error("invalid qq identity signature")
    return user_id, request_id


def authenticate_token(token: str) -> AuthContext | None:
    token = token.strip()
    if not token or len(token) > 512:
        return None
    # 新配置优先；旧 API_TOKEN 兼容为 owner/internal。
    for role, field in ROLE_TOKENS.items():
        configured = getattr(settings, field, "")
        if configured and _safe_eq(token, configured):
            return AuthContext(token, role, _subject_for_role(role))
    if settings.api_token and _safe_eq(token, settings.api_token):
        return AuthContext(token, "owner", _owner_subject())
    return None


def get_auth(request: Request) -> AuthContext:
    ctx = getattr(request.state, "auth", None)
    if ctx is None:
        # 未配置任何 token 时保持旧的内网开放策略，默认视为 owner。
        if not any(getattr(settings, name, "") for name in ("api_token", *ROLE_TOKENS.values())):
            return AuthContext("", "owner", _owner_subject())
        raise HTTPException(status_code=401, detail="unauthorized")
    return ctx


def require_roles(request: Request, *roles: str) -> AuthContext:
    ctx = get_auth(request)
    if ctx.role not in roles:
        raise HTTPException(status_code=403, detail="forbidden")
    return ctx
