"""群聊会话心流：决定小月的活跃度与说话节奏。

这是 MaiBot 心流/发言时机思路的独立重写，不复制外部项目代码或提示词。
只保存按群作用域的抽象状态，不保存原始消息、QQ 号或个人画像。
"""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import Any

MAX_GROUPS = 256
RECOVERY_PER_MINUTE = 0.02
MESSAGE_COST = 0.018
REPLY_COST = 0.07


@dataclass(frozen=True)
class HeartflowConfig:
    enabled: bool = True
    talk_frequency: float = 0.0
    silence_seconds: float = 60.0
    max_consecutive_replies: int = 2

    def normalized(self) -> "HeartflowConfig":
        try:
            frequency = max(0.0, min(1.0, float(self.talk_frequency)))
        except (TypeError, ValueError):
            frequency = 0.0
        try:
            silence = max(0.0, float(self.silence_seconds))
        except (TypeError, ValueError):
            silence = 60.0
        try:
            consecutive = max(1, int(self.max_consecutive_replies))
        except (TypeError, ValueError):
            consecutive = 2
        return HeartflowConfig(
            enabled=bool(self.enabled),
            talk_frequency=frequency,
            silence_seconds=silence,
            max_consecutive_replies=consecutive,
        )


@dataclass
class HeartflowState:
    message_count: int = 0
    directed_count: int = 0
    reply_count: int = 0
    last_message_at: float | None = None
    last_reply_at: float | None = None
    energy: float = 1.0
    atmosphere: str = "casual"
    consecutive_replies: int = 0
    last_action: str = "idle"


@dataclass(frozen=True)
class HeartflowDecision:
    allowed: bool
    propensity: float
    threshold: float
    reason: str
    action: str = "ignore"

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": bool(self.allowed),
            "propensity": round(max(0.0, min(1.0, self.propensity)), 3),
            "threshold": round(max(0.0, min(1.0, self.threshold)), 3),
            "reason": self.reason,
            "action": self.action,
        }


class Heartflow:
    def __init__(self, *, max_groups: int = MAX_GROUPS) -> None:
        self._states: OrderedDict[str, HeartflowState] = OrderedDict()
        self._max_groups = max(8, int(max_groups))
        self._lock = RLock()

    @staticmethod
    def _now(value: float | None) -> float:
        return time.time() if value is None else float(value)

    def _state(self, group_id: str) -> HeartflowState:
        group = str(group_id or "").strip()
        if not group:
            raise ValueError("group_id is required")
        state = self._states.setdefault(group, HeartflowState())
        self._states.move_to_end(group)
        while len(self._states) > self._max_groups:
            self._states.popitem(last=False)
        return state

    @staticmethod
    def _recover(state: HeartflowState, current: float) -> None:
        if state.last_message_at is not None:
            idle_minutes = max(0.0, current - state.last_message_at) / 60.0
            state.energy = min(1.0, state.energy + idle_minutes * RECOVERY_PER_MINUTE)

    def observe_message(
        self,
        group_id: str,
        *,
        directed: bool,
        atmosphere: str = "casual",
        now: float | None = None,
    ) -> None:
        current = self._now(now)
        with self._lock:
            state = self._state(group_id)
            self._recover(state, current)
            state.message_count += 1
            state.directed_count += int(bool(directed))
            state.last_message_at = current
            state.energy = max(0.0, state.energy - MESSAGE_COST)
            state.atmosphere = str(atmosphere or "casual").strip()[:32] or "casual"

    def decide(
        self,
        group_id: str,
        *,
        score: float,
        directed: bool,
        config: HeartflowConfig,
        now: float | None = None,
    ) -> HeartflowDecision:
        current = self._now(now)
        normalized = config.normalized()
        with self._lock:
            state = self._state(group_id)
            self._recover(state, current)
            if directed:
                return HeartflowDecision(True, 1.0, 0.0, "directed", "answer")
            if not normalized.enabled or normalized.talk_frequency <= 0.0:
                return HeartflowDecision(False, 0.0, 1.0, "frequency_disabled")
            if state.last_reply_at is not None:
                elapsed = current - state.last_reply_at
                if elapsed < normalized.silence_seconds:
                    return HeartflowDecision(False, 0.0, 1.0, "silence_window")
            if state.consecutive_replies >= normalized.max_consecutive_replies:
                return HeartflowDecision(False, 0.0, 1.0, "consecutive_reply_limit")

            # 频率越高，允许的最低兴趣分越低；仍由现有评分器和小时闸门兜底。
            threshold = max(0.55, 0.95 - normalized.talk_frequency * 0.30)
            energy_factor = 0.65 + 0.35 * max(0.0, min(1.0, state.energy))
            propensity = max(0.0, min(1.0, float(score))) * energy_factor
            allowed = propensity >= threshold
            return HeartflowDecision(
                allowed,
                propensity,
                threshold,
                "heartflow_ready" if allowed else "heartflow_low_propensity",
                "interject" if allowed else "ignore",
            )

    def record_reply(self, group_id: str, *, action: str = "answer", now: float | None = None) -> None:
        current = self._now(now)
        with self._lock:
            state = self._state(group_id)
            self._recover(state, current)
            state.reply_count += 1
            state.last_reply_at = current
            state.energy = max(0.0, state.energy - REPLY_COST)
            state.consecutive_replies += 1
            state.last_action = str(action or "answer").strip()[:24] or "answer"

    def record_silence(self, group_id: str, *, now: float | None = None) -> None:
        current = self._now(now)
        with self._lock:
            state = self._state(group_id)
            self._recover(state, current)
            state.consecutive_replies = 0
            state.last_action = "ignore"

    def snapshot(self, group_id: str, *, now: float | None = None) -> dict[str, Any]:
        current = self._now(now)
        group = str(group_id or "").strip()
        if not group:
            return {}
        with self._lock:
            state = self._states.get(group)
            if state is None:
                return {}
            self._recover(state, current)
            self._states.move_to_end(group)
            return {
                "message_count": state.message_count,
                "directed_count": state.directed_count,
                "reply_count": state.reply_count,
                "energy": round(max(0.0, min(1.0, state.energy)), 3),
                "atmosphere": state.atmosphere,
                "consecutive_replies": state.consecutive_replies,
                "last_action": state.last_action,
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
            energy_label = "精力偏低，表达尽量短，不主动展开"
        elif energy < 0.5:
            energy_label = "精力一般，优先回应明确问题"
        else:
            energy_label = "精力正常，可以自然接话"
        atmosphere = str(state.get("atmosphere") or "casual")
        caution = "；气氛偏紧张，先克制表达" if atmosphere in {"tense", "emotional"} else ""
        return f"小月当前心流：{energy_label}；群内气氛为{atmosphere}{caution}。"

    def reset(self, group_id: str | None = None) -> None:
        with self._lock:
            if group_id is None:
                self._states.clear()
            else:
                self._states.pop(str(group_id).strip(), None)


heartflow = Heartflow()


def config_from_settings(settings: Any, *, interject_enabled: bool = False) -> HeartflowConfig:
    frequency = getattr(settings, "group_talk_frequency", 0.0)
    # 兼容旧配置：显式打开主动插话即代表允许其进入心流，不要求再补一项频率。
    if interject_enabled:
        try:
            if float(frequency) <= 0.0:
                frequency = 1.0
        except (TypeError, ValueError):
            frequency = 1.0
    return HeartflowConfig(
        enabled=getattr(settings, "group_heartflow_enabled", True),
        talk_frequency=frequency,
        silence_seconds=getattr(settings, "group_silence_seconds", 60.0),
        max_consecutive_replies=getattr(settings, "group_max_consecutive_replies", 2),
    ).normalized()


def observe_message(group_id: str, *, directed: bool, atmosphere: str = "casual", now: float | None = None) -> None:
    heartflow.observe_message(group_id, directed=directed, atmosphere=atmosphere, now=now)


def decide(group_id: str, *, score: float, directed: bool, config: HeartflowConfig, now: float | None = None) -> HeartflowDecision:
    return heartflow.decide(group_id, score=score, directed=directed, config=config, now=now)


def record_reply(group_id: str, *, action: str = "answer", now: float | None = None) -> None:
    heartflow.record_reply(group_id, action=action, now=now)


def record_silence(group_id: str, *, now: float | None = None) -> None:
    heartflow.record_silence(group_id, now=now)


def snapshot(group_id: str, *, now: float | None = None) -> dict[str, Any]:
    return heartflow.snapshot(group_id, now=now)


def get_injection(group_id: str, *, now: float | None = None) -> str:
    return heartflow.get_injection(group_id, now=now)


def reset(group_id: str | None = None) -> None:
    heartflow.reset(group_id)
