"""网关侧 QQ 身份签名；格式必须与 ``server.app.auth`` 保持一致。"""
from __future__ import annotations

import hashlib
import hmac


def qq_identity_payload(user_id: str, timestamp: str | int, request_id: str) -> bytes:
    return f"{str(user_id).strip()}\n{str(timestamp).strip()}\n{str(request_id).strip()}".encode()


def sign_qq_identity(secret: str, user_id: str, timestamp: str | int, request_id: str) -> str:
    return hmac.new(
        str(secret).encode(),
        qq_identity_payload(user_id, timestamp, request_id),
        hashlib.sha256,
    ).hexdigest()


__all__ = ["qq_identity_payload", "sign_qq_identity"]
