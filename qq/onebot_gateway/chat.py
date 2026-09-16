"""现有聊天 HTTP 契约的网关客户端。"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from .auth import sign_qq_identity
from .config import GatewaySettings
from .event import OneBotMessage


class ChatUpstreamError(RuntimeError):
    """聊天服务调用失败。"""


@dataclass(frozen=True, slots=True)
class ChatResult:
    reply: str
    interaction: dict[str, Any]


class ChatClient:
    def __init__(
        self,
        settings: GatewaySettings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._client = client
        self._owns_client = client is None

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.request_timeout_seconds)
        return self._client

    def _headers(self, message: OneBotMessage, request_id: str, *, owner: bool) -> dict[str, str]:
        token = self.settings.owner_api_token if owner else self.settings.qq_api_token
        if not token:
            raise ChatUpstreamError("聊天鉴权配置缺失")
        headers = {"Authorization": f"Bearer {token}"}
        if not owner:
            timestamp = str(int(time.time()))
            headers.update({
                "X-QQ-User-ID": message.user_id,
                "X-QQ-Timestamp": timestamp,
                "X-QQ-Request-ID": request_id,
                "X-QQ-Signature": sign_qq_identity(
                    self.settings.identity_secret,
                    message.user_id,
                    timestamp,
                    request_id,
                ),
            })
        return headers

    async def chat(self, message: OneBotMessage, *, group_directed: bool = False) -> ChatResult:
        owner = message.message_type == "private" and bool(
            self.settings.owner_id and message.user_id == self.settings.owner_id
        )
        request_id = message.request_id
        body: dict[str, Any] = {
            "message": message.text,
            "request_id": request_id,
            "user_id": message.user_id,
        }
        if message.group_id:
            body.update({
                "group_id": message.group_id,
                "group_directed": bool(group_directed),
            })
        try:
            response = await self._http_client().post(
                f"{self.settings.server_api_base}/api/chat",
                json=body,
                headers=self._headers(message, request_id, owner=owner),
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise ChatUpstreamError("聊天服务请求失败") from exc
        if not isinstance(payload, dict):
            raise ChatUpstreamError("聊天服务返回格式错误")
        reply = str(payload.get("reply") or "").strip()
        interaction = payload.get("interaction")
        if not isinstance(interaction, dict):
            interaction = {}
        return ChatResult(reply=reply, interaction=interaction)

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


__all__ = ["ChatClient", "ChatResult", "ChatUpstreamError"]
