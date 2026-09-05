"""统一认证上下文与角色权限。"""
import secrets
from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.config import settings


@dataclass(frozen=True)
class AuthContext:
    token: str
    role: str
    subject: str | None = None

ROLE_TOKENS = {
    "owner": "owner_api_token",
    "internal": "internal_api_token",
    "collector": "collector_api_token",
    "executor": "executor_api_token",
    "qq": "qq_api_token",
}

def _safe_eq(a: str, b: str) -> bool:
    """compare_digest 的字节版封装：str 版对非 ASCII 直接抛 TypeError，
    曾让任意未认证请求用一个中文 token 就打成 500。"""
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def authenticate_token(token: str) -> AuthContext | None:
    token = token.strip()
    if not token or len(token) > 512:
        return None
    # 新配置优先；旧 API_TOKEN 兼容为 owner/internal。
    for role, field in ROLE_TOKENS.items():
        configured = getattr(settings, field, "")
        if configured and _safe_eq(token, configured):
            return AuthContext(token, role)
    if settings.api_token and _safe_eq(token, settings.api_token):
        return AuthContext(token, "owner")
    return None

def get_auth(request: Request) -> AuthContext:
    ctx = getattr(request.state, "auth", None)
    if ctx is None:
        # 未配置任何 token 时保持旧的内网开放策略，默认视为 owner。
        if not any(getattr(settings, name, "") for name in ("api_token", *ROLE_TOKENS.values())):
            return AuthContext("", "owner")
        raise HTTPException(status_code=401, detail="unauthorized")
    return ctx

def require_roles(request: Request, *roles: str) -> AuthContext:
    ctx = get_auth(request)
    if ctx.role not in roles:
        raise HTTPException(status_code=403, detail="forbidden")
    return ctx
