"""OneBot HTTP 网关应用与消息编排。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from .chat import ChatClient, ChatConfigError, ChatUpstreamError
from .config import GatewaySettings, settings as default_settings
from .event import OneBotMessage
from .gate import (
    ContextWindowStore,
    DeliveryCache,
    FollowupStore,
    GroupReplyLimiter,
    ReplyReservation,
    is_group_directed,
)
from .onebot import OneBotClient, OneBotError

logger = logging.getLogger("assistant.qq_gateway")


class Gateway:
    """把 OneBot 事件转换为现有聊天 HTTP 契约。"""

    def __init__(
        self,
        config: GatewaySettings,
        *,
        onebot: OneBotClient | None = None,
        chat: ChatClient | None = None,
    ) -> None:
        self.config = config
        self.onebot = onebot or OneBotClient(
            config.onebot_api_url,
            config.onebot_token,
            timeout=config.request_timeout_seconds,
        )
        self.chat = chat or ChatClient(config)
        self.limiter = GroupReplyLimiter()
        self.followups = FollowupStore(
            default_expires_in=config.followup_window_seconds,
            default_max_messages=config.followup_max_messages,
        )
        self.context_windows = ContextWindowStore(
            default_expires_in=config.context_window_seconds,
            default_max_messages=config.context_max_messages,
            default_max_noncontinuations=config.context_max_noncontinuations,
        )
        self.deliveries = DeliveryCache()
        self._inflight: dict[str, Any] = {}

    def authorize(self, request: Request) -> None:
        expected = self.config.inbound_token
        if not expected:
            if not self.config._is_loopback_host():
                raise HTTPException(status_code=401, detail="QQ 网关入站鉴权未配置")
            return
        authorization = request.headers.get("authorization", "")
        bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        provided = bearer or request.headers.get("x-onebot-token", "").strip()
        if provided != expected:
            raise HTTPException(status_code=401, detail="unauthorized")

    async def handle(self, payload: Any) -> dict[str, Any]:
        message = OneBotMessage.from_payload(payload)
        if message is None:
            return {"status": "ignored", "reason": "unsupported_event"}
        if message.has_media:
            return {"status": "ignored", "reason": "media_not_supported"}
        if not message.text:
            return {"status": "ignored", "reason": "empty_message"}
        if len(message.text) > self.config.max_message_chars:
            return {"status": "ignored", "reason": "message_too_long"}
        request_id = message.request_id
        if self.deliveries.seen(request_id):
            return {"status": "ok", "duplicate": True, "sent": False}
        current = self._inflight.get(request_id)
        if current is not None:
            return await current
        task = self._create_task(message)
        self._inflight[request_id] = task
        try:
            return await task
        finally:
            if self._inflight.get(request_id) is task:
                self._inflight.pop(request_id, None)

    def _create_task(self, message: OneBotMessage):
        import asyncio

        return asyncio.create_task(self._process(message))

    async def _process(self, message: OneBotMessage) -> dict[str, Any]:
        if message.message_type == "group":
            return await self._process_group(message)
        return await self._process_private(message)

    def _self_id(self, message: OneBotMessage) -> str:
        configured = self.config.configured_self_id
        if configured:
            if not message.self_id or message.self_id != configured:
                return ""
            return configured
        return message.self_id

    async def _process_group(self, message: OneBotMessage) -> dict[str, Any]:
        if not self.config.group_allowed(message.group_id):
            return {"status": "ignored", "reason": "group_not_allowed"}
        directed = await is_group_directed(
            message,
            self_id=self._self_id(message),
            require_mention=self.config.group_require_mention,
            prefix=self.config.group_trigger_prefix,
            reply_sender=self.onebot.get_message_sender,
        )
        context_active = False
        if directed:
            self.followups.clear(message.group_id, message.user_id)
            self.context_windows.close(message.group_id, message.user_id)
        elif (
            self.config.context_window_enabled
            and self.context_windows.consume(message.group_id, message.user_id)
        ):
            context_active = True
        elif self.config.followup_enabled and self.followups.consume(message.group_id, message.user_id):
            directed = True
        elif not self.config.group_interject_enabled:
            return {"status": "ignored", "reason": "not_directed"}

        reservation = None
        if directed or not context_active:
            reservation = self.limiter.reserve(
                message.group_id,
                cooldown_seconds=self.config.group_cooldown_seconds,
                max_replies_per_hour=self.config.group_max_replies_per_hour,
            )
            if reservation is None:
                return {"status": "ignored", "reason": "group_rate_limited"}
        return await self._chat_and_send(
            message,
            directed=directed,
            context_active=context_active,
            reservation=reservation,
        )

    async def _process_private(self, message: OneBotMessage) -> dict[str, Any]:
        return await self._chat_and_send(
            message,
            directed=False,
            context_active=False,
            reservation=None,
        )

    async def _chat_and_send(
        self,
        message: OneBotMessage,
        *,
        directed: bool,
        context_active: bool,
        reservation: ReplyReservation | None,
    ) -> dict[str, Any]:
        try:
            chat_kwargs = {"group_directed": directed}
            if context_active:
                chat_kwargs["group_context_active"] = True
            result = await self.chat.chat(message, **chat_kwargs)
        except ChatConfigError as exc:
            # 服务器侧鉴权/配置错误：重试无用，也不该让群成员看到内部故障提示。
            self.limiter.release(reservation)
            logger.error("QQ 网关鉴权/配置错误，已静默：%s", type(exc).__name__)
            return {"status": "error", "handled": True, "directed": directed, "sent": False}
        except ChatUpstreamError as exc:
            logger.warning("QQ 聊天上游失败：%s", type(exc).__name__)
            return await self._send_error(message, reservation)
        context_decision = result.interaction.get("context_window") if isinstance(result.interaction, dict) else None
        if message.group_id and context_active:
            if isinstance(context_decision, dict) and context_decision.get("open") is False:
                self.context_windows.close(message.group_id, message.user_id)
            else:
                self.context_windows.observe(message.group_id, message.user_id, context_decision)
        if not result.reply:
            self.limiter.release(reservation)
            self.deliveries.mark(message.request_id)
            if message.group_id and directed:
                self.followups.clear(message.group_id, message.user_id)
                if self.config.context_window_enabled:
                    window = result.interaction.get("context_window") if isinstance(result.interaction, dict) else None
                    if not isinstance(window, dict) or window.get("open", True):
                        self.context_windows.open(
                            message.group_id,
                            message.user_id,
                            expires_in=window.get("expires_in") if isinstance(window, dict) else None,
                            max_messages=window.get("max_messages") if isinstance(window, dict) else None,
                            max_noncontinuations=(
                                window.get("max_noncontinuations") if isinstance(window, dict) else None
                            ),
                        )
            return {"status": "ok", "handled": True, "directed": directed, "sent": False}
        if reservation is None and message.group_id and not directed:
            reservation = self.limiter.reserve(
                message.group_id,
                cooldown_seconds=self.config.group_cooldown_seconds,
                max_replies_per_hour=self.config.group_max_replies_per_hour,
            )
            if reservation is None:
                return {"status": "ignored", "reason": "group_rate_limited"}
        reply = result.reply[: self.config.max_reply_chars]
        try:
            if message.group_id:
                await self.onebot.send_group(message.group_id, reply)
            else:
                await self.onebot.send_private(message.user_id, reply)
        except OneBotError as exc:
            self.limiter.release(reservation)
            logger.warning("QQ 回复发送失败：%s", type(exc).__name__)
            return {"status": "error", "handled": True, "directed": directed, "sent": False}
        self.limiter.commit(reservation)
        self.deliveries.mark(message.request_id)
        if message.group_id and directed and self.config.followup_enabled:
            self.followups.remember(message.group_id, message.user_id, result.interaction)
        if message.group_id and directed and self.config.context_window_enabled:
            window = result.interaction.get("context_window") if isinstance(result.interaction, dict) else None
            if not isinstance(window, dict) or window.get("open", True):
                self.context_windows.open(
                    message.group_id,
                    message.user_id,
                    expires_in=window.get("expires_in") if isinstance(window, dict) else None,
                    max_messages=window.get("max_messages") if isinstance(window, dict) else None,
                    max_noncontinuations=(
                        window.get("max_noncontinuations") if isinstance(window, dict) else None
                    ),
                )
        return {"status": "ok", "handled": True, "directed": directed, "sent": True}

    async def _send_error(
        self,
        message: OneBotMessage,
        reservation: ReplyReservation | None,
    ) -> dict[str, Any]:
        if not self.config.send_error_reply:
            self.limiter.release(reservation)
            return {"status": "error", "handled": True, "sent": False}
        reply = "小月服务暂时不可达，请稍后再试。"
        try:
            if message.group_id:
                await self.onebot.send_group(message.group_id, reply)
            else:
                await self.onebot.send_private(message.user_id, reply)
        except OneBotError:
            self.limiter.release(reservation)
            return {"status": "error", "handled": True, "sent": False}
        self.limiter.commit(reservation)
        self.deliveries.mark(message.request_id)
        return {"status": "error", "handled": True, "sent": True}

    async def aclose(self) -> None:
        await self.onebot.aclose()
        await self.chat.aclose()


def create_app(
    gateway: Gateway | None = None,
    config: GatewaySettings | None = None,
) -> FastAPI:
    manager = gateway or Gateway(config or default_settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await manager.aclose()

    application = FastAPI(
        title="Personal AI Assistant QQ Gateway",
        version="1.0",
        lifespan=lifespan,
    )

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/onebot")
    async def receive_onebot(payload: dict[str, Any], request: Request) -> dict[str, Any]:
        manager.authorize(request)
        return await manager.handle(payload)

    return application


app = create_app()

__all__ = ["Gateway", "app", "create_app"]
