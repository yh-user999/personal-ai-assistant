"""NapCat OneBot v11 HTTP action 客户端。"""
from __future__ import annotations

from typing import Any

import httpx


class OneBotError(RuntimeError):
    """OneBot action 请求失败。"""


class OneBotClient:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        *,
        timeout: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.token = str(token or "").strip()
        self.timeout = max(1.0, float(timeout))
        self._client = client
        self._owns_client = client is None

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def call(self, action: str, params: dict[str, Any] | None = None) -> Any:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            response = await self._http_client().post(
                self.base_url,
                json={"action": action, "params": params or {}},
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise OneBotError("OneBot action 请求失败") from exc
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise OneBotError("OneBot action 返回失败")
        try:
            retcode = int(payload.get("retcode", -1))
        except (TypeError, ValueError) as exc:
            raise OneBotError("OneBot action 返回失败") from exc
        if retcode != 0:
            raise OneBotError("OneBot action 返回失败")
        return payload.get("data")

    async def get_message_sender(self, message_id: str) -> str | None:
        data = await self.call("get_msg", {"message_id": int(message_id)})
        if not isinstance(data, dict):
            return None
        sender = data.get("sender")
        if isinstance(sender, dict):
            value = sender.get("user_id")
        else:
            value = data.get("user_id")
        text = str(value or "").strip()
        return text if text.isdigit() else None

    async def send_group(self, group_id: str, message: str) -> None:
        await self.call("send_group_msg", {"group_id": int(group_id), "message": message})

    async def send_private(self, user_id: str, message: str) -> None:
        await self.call("send_private_msg", {"user_id": int(user_id), "message": message})

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


__all__ = ["OneBotClient", "OneBotError"]
