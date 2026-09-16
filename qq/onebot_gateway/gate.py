"""群聊唯一门禁：真实 @、短时续话和发送频率控制。"""
from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from threading import RLock
from typing import Any

from .event import OneBotMessage


async def is_group_directed(
    message: OneBotMessage,
    *,
    self_id: str,
    require_mention: bool,
    prefix: str,
    reply_sender: Callable[[str], Awaitable[str | None]],
) -> bool:
    """确定性判断群消息是否明确在叫机器人；任何不确定都返回 False。"""
    text = message.text
    if prefix and text.startswith(prefix):
        return True
    if not require_mention:
        return True
    target = str(self_id or "").strip()
    if not target:
        return False
    if target in message.at_targets:
        return True
    for reply_id in message.reply_ids:
        try:
            sender_id = await reply_sender(reply_id)
        except Exception:  # noqa: BLE001
            return False
        if str(sender_id or "").strip() == target:
            return True
    return False


@dataclass(frozen=True, slots=True)
class ReplyReservation:
    group_id: str
    timestamp: float
    token: str


class GroupReplyLimiter:
    """单进程群回复限流；只对成功发送保留小时配额。"""

    def __init__(self, *, maximum_groups: int = 4096) -> None:
        self.maximum_groups = max(1, int(maximum_groups))
        self._lock = RLock()
        self._timestamps: dict[str, list[float]] = {}
        self._reservations: dict[str, ReplyReservation] = {}

    def reserve(
        self,
        group_id: str,
        *,
        cooldown_seconds: float,
        max_replies_per_hour: int,
        now: float | None = None,
    ) -> ReplyReservation | None:
        group = str(group_id or "").strip()
        if not group:
            return None
        current = time.monotonic() if now is None else float(now)
        cooldown = max(0.0, float(cooldown_seconds))
        hourly_limit = max(1, int(max_replies_per_hour))
        with self._lock:
            self._prune(current)
            if group not in self._timestamps and len(self._timestamps) >= self.maximum_groups:
                return None
            events = self._timestamps.setdefault(group, [])
            if events and current - events[-1] < cooldown:
                return None
            if len(events) >= hourly_limit:
                return None
            reservation = ReplyReservation(group, current, uuid.uuid4().hex)
            events.append(current)
            self._reservations[reservation.token] = reservation
            return reservation

    def _prune(self, current: float) -> None:
        cutoff = current - 3600
        reserved_groups = {reservation.group_id for reservation in self._reservations.values()}
        for group, events in list(self._timestamps.items()):
            events[:] = [stamp for stamp in events if stamp > cutoff]
            if not events and group not in reserved_groups:
                self._timestamps.pop(group, None)

    def commit(self, reservation: ReplyReservation | None) -> None:
        if reservation is None:
            return
        with self._lock:
            self._reservations.pop(reservation.token, None)

    def release(self, reservation: ReplyReservation | None) -> None:
        if reservation is None:
            return
        with self._lock:
            stored = self._reservations.pop(reservation.token, None)
            if stored is None:
                return
            events = self._timestamps.get(stored.group_id, [])
            try:
                events.remove(stored.timestamp)
            except ValueError:
                pass
            if not events:
                self._timestamps.pop(stored.group_id, None)

    def reset(self) -> None:
        with self._lock:
            self._timestamps.clear()
            self._reservations.clear()


@dataclass(slots=True)
class _Followup:
    expires_at: float
    remaining: int


class FollowupStore:
    """只保存群/用户的短时计数，不保存原始消息或回复正文。"""

    def __init__(
        self,
        *,
        default_expires_in: float = 90.0,
        default_max_messages: int = 1,
        maximum_items: int = 4096,
    ) -> None:
        self.default_expires_in = max(1.0, min(600.0, float(default_expires_in)))
        self.default_max_messages = max(1, min(10, int(default_max_messages)))
        self.maximum_items = max(1, int(maximum_items))
        self._lock = RLock()
        self._items: dict[tuple[str, str], _Followup] = {}

    def remember(self, group_id: str, user_id: str, interaction: Any, *, now: float | None = None) -> None:
        if not isinstance(interaction, dict):
            return
        followup = interaction.get("followup")
        if not isinstance(followup, dict):
            return
        try:
            expires_in = max(
                1.0,
                min(self.default_expires_in, float(followup.get("expires_in", self.default_expires_in))),
            )
            max_messages = max(
                1,
                min(self.default_max_messages, int(followup.get("max_messages", self.default_max_messages))),
            )
        except (TypeError, ValueError):
            return
        key = (str(group_id or "").strip(), str(user_id or "").strip())
        if not all(key):
            return
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            self._prune(current)
            if key not in self._items and len(self._items) >= self.maximum_items:
                return
            self._items[key] = _Followup(current + expires_in, max_messages)

    def _prune(self, current: float) -> None:
        for key, item in list(self._items.items()):
            if item.expires_at <= current or item.remaining <= 0:
                self._items.pop(key, None)

    def consume(self, group_id: str, user_id: str, *, now: float | None = None) -> bool:
        key = (str(group_id or "").strip(), str(user_id or "").strip())
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            self._prune(current)
            item = self._items.get(key)
            if item is None:
                return False
            item.remaining -= 1
            if item.remaining <= 0:
                self._items.pop(key, None)
            return True

    def clear(self, group_id: str, user_id: str) -> None:
        with self._lock:
            self._items.pop((str(group_id or "").strip(), str(user_id or "").strip()), None)

    def reset(self) -> None:
        with self._lock:
            self._items.clear()


class DeliveryCache:
    """有界成功投递缓存，防止 NapCat 重试同一事件造成重复发送。"""

    def __init__(self, *, ttl_seconds: float = 86400.0, maximum: int = 4096) -> None:
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.maximum = max(1, int(maximum))
        self._lock = RLock()
        self._items: OrderedDict[str, float] = OrderedDict()

    def seen(self, request_id: str, *, now: float | None = None) -> bool:
        key = str(request_id or "").strip()
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            self._prune(current)
            if key not in self._items:
                return False
            self._items.move_to_end(key)
            return True

    def mark(self, request_id: str, *, now: float | None = None) -> None:
        key = str(request_id or "").strip()
        if not key:
            return
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            self._prune(current)
            self._items[key] = current
            self._items.move_to_end(key)
            while len(self._items) > self.maximum:
                self._items.popitem(last=False)

    def _prune(self, current: float) -> None:
        cutoff = current - self.ttl_seconds
        while self._items:
            _key, timestamp = next(iter(self._items.items()))
            if timestamp > cutoff:
                break
            self._items.popitem(last=False)

    def reset(self) -> None:
        with self._lock:
            self._items.clear()


__all__ = [
    "DeliveryCache",
    "FollowupStore",
    "GroupReplyLimiter",
    "ReplyReservation",
    "is_group_directed",
]
