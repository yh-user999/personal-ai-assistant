"""小月在群聊中的短期自状态。

只保留每群的计数、时间和有限状态，不保存消息正文、用户 ID 或 prompt。状态
用于调整是否抢话和表达语气，不能授予权限，也不能替代安全门禁。
"""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import Any

MAX_GROUPS = 256
RECOVERY_PER_MINUTE = 0.015
MESSAGE_COST = 0.025
REPLY_COST = 0.08


@dataclass
class _State:
    message_count: int = 0
    reply_count: int = 0
    last_message_at: float | None = None
    last_reply_at: float | None = None
    energy: float = 1.0
    atmosphere: str = "casual"


class RobotState:
    def __init__(self, *, max_groups: int = MAX_GROUPS) -> None:
        self._states: OrderedDict[str, _State] = OrderedDict()
        self._max_groups = max(8, int(max_groups))
        self._lock = RLock()

    @staticmethod
    def _now(value: float | None) -> float:
        return time.time() if value is None else float(value)

    def _state(self, group_id: str) -> _State:
        group = str(group_id or "").strip()
        if not group:
            raise ValueError("group_id is required")
        state = self._states.setdefault(group, _State())
        self._states.move_to_end(group)
        while len(self._states) > self._max_groups:
            self._states.popitem(last=False)
        return state

    @staticmethod
    def _recover(state: _State, current: float) -> None:
        if state.last_message_at is not None:
            idle_minutes = max(0.0, current - state.last_message_at) / 60.0
            state.energy = min(1.0, state.energy + idle_minutes * RECOVERY_PER_MINUTE)

    def observe_message(
        self,
        group_id: str,
        *,
        atmosphere: str = "casual",
        now: float | None = None,
    ) -> None:
        current = self._now(now)
        with self._lock:
            state = self._state(group_id)
            self._recover(state, current)
            state.message_count += 1
            state.last_message_at = current
            state.energy = max(0.0, state.energy - MESSAGE_COST)
            state.atmosphere = str(atmosphere or "casual").strip()[:32] or "casual"

    def record_reply(self, group_id: str, *, now: float | None = None) -> None:
        current = self._now(now)
        with self._lock:
            state = self._state(group_id)
            self._recover(state, current)
            state.reply_count += 1
            state.last_reply_at = current
            state.energy = max(0.0, state.energy - REPLY_COST)

    def snapshot(self, group_id: str, *, now: float | None = None) -> dict[str, Any]:
        current = self._now(now)
        group = str(group_id or "").strip()
        if not group:
            return {}
        with self._lock:
            state = self._states.get(group)
            if state is None:
                return {}
            self._states.move_to_end(group)
            self._recover(state, current)
            return {
                "message_count": state.message_count,
                "reply_count": state.reply_count,
                "energy": round(max(0.0, min(1.0, state.energy)), 3),
                "atmosphere": state.atmosphere,
                "has_recent_reply": bool(
                    state.last_reply_at is not None and current - state.last_reply_at <= 180
                ),
            }

    def get_injection(self, group_id: str, *, now: float | None = None) -> str:
        state = self.snapshot(group_id, now=now)
        if not state:
            return ""
        energy = float(state.get("energy", 1.0))
        if energy < 0.25:
            energy_label = "精力偏低，表达尽量简短"
        elif energy < 0.5:
            energy_label = "精力一般，避免主动展开"
        else:
            energy_label = "精力正常，可以自然接话"
        atmosphere = str(state.get("atmosphere") or "casual")
        caution = "；当前气氛偏紧张，先谨慎观察" if atmosphere in {"tense", "emotional"} else ""
        return f"小月当前群聊状态：{energy_label}；最近气氛为{atmosphere}{caution}。"

    def reset(self, group_id: str | None = None) -> None:
        with self._lock:
            if group_id is None:
                self._states.clear()
            else:
                self._states.pop(str(group_id).strip(), None)


state = RobotState()


def observe_group_message(group_id: str, *, atmosphere: str = "casual", now: float | None = None) -> None:
    state.observe_message(group_id, atmosphere=atmosphere, now=now)


def record_group_reply(group_id: str, *, now: float | None = None) -> None:
    state.record_reply(group_id, now=now)


def get_group_injection(group_id: str, *, now: float | None = None) -> str:
    return state.get_injection(group_id, now=now)


def snapshot(group_id: str, *, now: float | None = None) -> dict[str, Any]:
    return state.snapshot(group_id, now=now)


def reset(group_id: str | None = None) -> None:
    state.reset(group_id)
