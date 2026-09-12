"""群聊主动插话的确定性评分与频率闸门。

本模块不调用 LLM、不写数据库、不保存原始消息。评分结果只决定“是否值得
抢话”，事实回答、工具调用、权限和安全策略仍由现有聊天流水线负责。
"""
from __future__ import annotations

import re
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from threading import RLock
from typing import Any

SOCIAL_ACTION = "interject"
_IGNORE_ACTION = "ignore"

_QUESTION_RE = re.compile(
    r"[?？]|吗[？?。！!\s]*$|(?:怎么|为什么|是否|能不能|有没有|哪个|哪些|什么|建议|推荐)"
)
_BANTER_RE = re.compile(r"哈哈+|笑死|绷不住|绝了|离谱|好家伙|太真实|破防|666|6{2,}")
_CELEBRATION_RE = re.compile(r"太爽|好耶|恭喜|成功|赢了|舒服|拿下|搞定")
_EMOTION_RE = re.compile(r"焦虑|难受|崩溃|烦|累|沮丧|生气|压力|睡不着|委屈|撑不住|绝望")
_COMMAND_RE = re.compile(
    r"(?:帮我|请|麻烦|直接|立刻|赶紧)?(?:执行|运行|删除|修改|发送|打开|关闭|重启)"
    r".{0,12}(?:脚本|命令|文件|服务|生产|权限|一下|一下子)"
    r"|(?:帮我|请|麻烦|直接|立刻|赶紧)部署.{0,12}(?:服务|项目|生产|一下)"
)
_SENSITIVE_RE = re.compile(
    r"(?:身份证|手机号|电话号码|住址|家庭住址|密码|口令|token|密钥|银行卡|收入|工资|性取向|宗教|政治立场)",
    re.IGNORECASE,
)
_CRISIS_RE = re.compile(r"自杀|自残|不想活|结束生命|伤害自己|活不下去")

_FACTOR_WEIGHTS = {
    "topic_fit": 0.25,
    "social_signal": 0.20,
    "novelty": 0.15,
    "conversation_gap": 0.15,
    "scene_fit": 0.15,
    "message_quality": 0.10,
}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _grams(text: str) -> set[str]:
    clean = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", text or "").casefold()
    if len(clean) < 2:
        return {clean} if clean else set()
    return {clean[i : i + 2] for i in range(len(clean) - 1)}


def _overlap(left: str, right: str) -> float:
    a, b = _grams(left), _grams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, len(a | b))


def _safe_history(history: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    out = []
    for item in (history or [])[-8:]:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("content") or "").split())[:500]
        if text:
            out.append({"role": str(item.get("role") or "user")[:16], "content": text})
    return out


@dataclass(frozen=True)
class InterjectionConfig:
    """主动插话的可调参数；所有边界在 ``normalized`` 中收敛。"""

    enabled: bool = False
    shadow_only: bool = True
    threshold: float = 0.72
    cooldown_seconds: float = 90.0
    hourly_limit: int = 6
    min_gap_messages: int = 2
    max_groups: int = 256

    def normalized(self) -> "InterjectionConfig":
        return InterjectionConfig(
            enabled=bool(self.enabled),
            shadow_only=bool(self.shadow_only),
            threshold=_clamp(self.threshold, 0.0, 1.0),
            cooldown_seconds=max(0.0, float(self.cooldown_seconds)),
            hourly_limit=max(1, int(self.hourly_limit)),
            min_gap_messages=max(0, int(self.min_gap_messages)),
            max_groups=max(8, int(self.max_groups)),
        )


@dataclass(frozen=True)
class SocialScore:
    score: float
    eligible: bool
    action: str = _IGNORE_ACTION
    factors: dict[str, float] = field(default_factory=dict)
    penalties: dict[str, float] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()

    @property
    def would_interject(self) -> bool:
        return self.eligible and self.action == SOCIAL_ACTION

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": round(_clamp(self.score), 3),
            "eligible": bool(self.eligible),
            "action": self.action,
            "factors": {key: round(_clamp(value), 3) for key, value in self.factors.items()},
            "penalties": {key: round(_clamp(value), 3) for key, value in self.penalties.items()},
            "reasons": list(self.reasons)[:8],
        }


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    would_allow: bool
    reason: str
    cooldown_remaining: float = 0.0
    hourly_count: int = 0
    message_gap: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": bool(self.allowed),
            "would_allow": bool(self.would_allow),
            "reason": self.reason,
            "cooldown_remaining": round(max(0.0, self.cooldown_remaining), 3),
            "hourly_count": max(0, int(self.hourly_count)),
            "message_gap": max(0, int(self.message_gap)),
        }


@dataclass
class _GroupGateState:
    message_seq: int = 0
    last_sent_at: float | None = None
    last_sent_seq: int | None = None
    sent_at: deque[float] = field(default_factory=deque)
    last_seen_at: float = 0.0


class GroupInterjectGate:
    """按群隔离的进程内频率闸门。

    当前服务按单进程运行，因此不引入数据库计数；状态只含时间、计数和序号，
    不含消息、用户 ID 或 prompt。多进程部署时应将此类替换成 SQLite 原子计数。
    """

    def __init__(self, *, max_groups: int = 256) -> None:
        self._states: OrderedDict[str, _GroupGateState] = OrderedDict()
        self._max_groups = max(8, int(max_groups))
        self._lock = RLock()

    def _state(self, group_id: str, current: float) -> _GroupGateState:
        group = str(group_id or "").strip()
        if not group:
            raise ValueError("group_id is required")
        state = self._states.setdefault(group, _GroupGateState())
        state.last_seen_at = current
        self._states.move_to_end(group)
        while len(self._states) > self._max_groups:
            self._states.popitem(last=False)
        return state

    @staticmethod
    def _prune_sent(state: _GroupGateState, current: float) -> None:
        while state.sent_at and current - state.sent_at[0] >= 3600:
            state.sent_at.popleft()

    def observe_message(self, group_id: str, *, now: float | None = None) -> int:
        current = _now(now)
        with self._lock:
            state = self._state(group_id, current)
            state.message_seq += 1
            self._prune_sent(state, current)
            return state.message_seq

    def check(
        self,
        group_id: str,
        score: SocialScore,
        *,
        config: InterjectionConfig | None = None,
        now: float | None = None,
    ) -> GateDecision:
        cfg = (config or InterjectionConfig()).normalized()
        current = _now(now)
        with self._lock:
            try:
                state = self._state(group_id, current)
            except ValueError:
                return GateDecision(False, False, "group_missing")
            self._prune_sent(state, current)
            hourly_count = len(state.sent_at)
            message_gap = (
                state.message_seq - state.last_sent_seq - 1
                if state.last_sent_seq is not None
                else state.message_seq
            )
            cooldown_remaining = 0.0
            if state.last_sent_at is not None:
                cooldown_remaining = max(0.0, cfg.cooldown_seconds - (current - state.last_sent_at))

            reason = "allowed"
            would_allow = True
            if not cfg.enabled:
                reason, would_allow = "disabled", False
            elif not score.eligible:
                reason, would_allow = "ineligible", False
            elif not score.would_interject or score.score < cfg.threshold:
                reason, would_allow = "score_below_threshold", False
            elif cooldown_remaining > 0:
                reason, would_allow = "cooldown", False
            elif state.last_sent_seq is not None and message_gap < cfg.min_gap_messages:
                reason, would_allow = "message_gap", False
            elif hourly_count >= cfg.hourly_limit:
                reason, would_allow = "hourly_limit", False

            allowed = bool(would_allow and not cfg.shadow_only)
            if would_allow and cfg.shadow_only:
                reason = "shadow_only"
            return GateDecision(
                allowed=allowed,
                would_allow=would_allow,
                reason=reason,
                cooldown_remaining=cooldown_remaining,
                hourly_count=hourly_count,
                message_gap=message_gap,
            )

    def record_sent(self, group_id: str, *, now: float | None = None) -> None:
        current = _now(now)
        with self._lock:
            state = self._state(group_id, current)
            self._prune_sent(state, current)
            state.last_sent_at = current
            state.last_sent_seq = state.message_seq
            state.sent_at.append(current)

    def reset(self, group_id: str | None = None) -> None:
        with self._lock:
            if group_id is None:
                self._states.clear()
            else:
                self._states.pop(str(group_id).strip(), None)

    def snapshot(self, group_id: str | None = None, *, now: float | None = None) -> dict[str, Any]:
        current = _now(now)
        with self._lock:
            keys = [str(group_id).strip()] if group_id else list(self._states)
            output: dict[str, Any] = {}
            for key in keys:
                state = self._states.get(key)
                if state is None:
                    continue
                self._prune_sent(state, current)
                output[key] = {
                    "message_seq": state.message_seq,
                    "last_sent_at": state.last_sent_at,
                    "last_sent_seq": state.last_sent_seq,
                    "hourly_count": len(state.sent_at),
                }
            return output


def _now(value: float | None) -> float:
    import time

    return time.time() if value is None else float(value)


def config_from_settings(settings: Any) -> InterjectionConfig:
    """从 Settings/测试替身读取参数，并对非法配置安全收敛。"""
    values = {
        "enabled": bool(
            getattr(settings, "group_social_enabled", True)
            and getattr(settings, "group_social_interject_enabled", False)
        ),
        "shadow_only": getattr(settings, "group_social_interject_shadow_only", True),
        "threshold": getattr(settings, "group_social_interject_threshold", 0.72),
        "cooldown_seconds": getattr(settings, "group_social_interject_cooldown_seconds", 90.0),
        "hourly_limit": getattr(settings, "group_social_interject_hourly_limit", 6),
        "min_gap_messages": getattr(settings, "group_social_interject_min_gap_messages", 2),
    }
    try:
        return InterjectionConfig(**values).normalized()
    except (TypeError, ValueError):
        return InterjectionConfig().normalized()


def score_group_interjection(
    message: str,
    history: list[dict[str, Any]] | None = None,
    scene: dict[str, Any] | None = None,
    robot_state: dict[str, Any] | None = None,
    *,
    directed: bool = False,
    threshold: float = 0.72,
) -> SocialScore:
    """为一条非直达群消息计算是否值得主动接话的分数。"""
    text = " ".join(str(message or "").split())[:800]
    recent = _safe_history(history)
    scene = scene or {}
    robot_state = robot_state or {}
    reasons: list[str] = []

    if directed:
        return SocialScore(0.0, False, _IGNORE_ACTION, reasons=("directed_message",))
    if not text:
        return SocialScore(0.0, False, _IGNORE_ACTION, reasons=("empty_message",))
    if _COMMAND_RE.search(text):
        return SocialScore(0.0, False, _IGNORE_ACTION, reasons=("command_like",))
    if _SENSITIVE_RE.search(text):
        return SocialScore(0.0, False, _IGNORE_ACTION, reasons=("sensitive_subject",))
    if _CRISIS_RE.search(text):
        return SocialScore(0.0, False, _IGNORE_ACTION, reasons=("crisis_signal",))

    recent_texts = [item["content"] for item in recent]
    overlaps = [_overlap(text, item) for item in recent_texts if item != text]
    max_overlap = max(overlaps, default=0.0)
    topic_fit = max_overlap if recent_texts else 0.35
    novelty = _clamp(1.0 - max_overlap)

    social_signal = 0.05
    if _QUESTION_RE.search(text):
        social_signal = max(social_signal, 0.9)
        reasons.append("question_signal")
    if _BANTER_RE.search(text):
        social_signal = max(social_signal, 0.8)
        reasons.append("banter_signal")
    if _CELEBRATION_RE.search(text):
        social_signal = max(social_signal, 0.75)
        reasons.append("celebration_signal")
    if _EMOTION_RE.search(text):
        social_signal = max(social_signal, 0.35)
        reasons.append("emotion_signal")

    has_recent_bot = (
        bool(scene["has_recent_bot_reply"])
        if "has_recent_bot_reply" in scene
        else any(item["role"] == "assistant" for item in recent[-2:])
    )
    conversation_gap = 0.15 if has_recent_bot else 0.85
    if not has_recent_bot:
        reasons.append("bot_gap")
    else:
        reasons.append("recent_bot_reply")

    atmosphere = str(scene.get("atmosphere") or "casual").strip().lower()
    scene_fit = {
        "celebration": 0.95,
        "casual": 0.85,
        "questioning": 0.80,
        "technical": 0.65,
        "emotional": 0.35,
        "tense": 0.15,
    }.get(atmosphere, 0.55)
    if atmosphere in {"emotional", "tense"}:
        reasons.append("sensitive_atmosphere")

    length = len(text)
    message_quality = 0.9 if 6 <= length <= 180 else 0.65 if length <= 400 else 0.25
    if length > 400:
        reasons.append("long_message")

    factors = {
        "topic_fit": topic_fit,
        "social_signal": social_signal,
        "novelty": novelty,
        "conversation_gap": conversation_gap,
        "scene_fit": scene_fit,
        "message_quality": message_quality,
    }
    weighted = sum(_FACTOR_WEIGHTS[key] * _clamp(value) for key, value in factors.items())
    penalties: dict[str, float] = {}
    if has_recent_bot:
        penalties["recent_bot_reply"] = 0.35
    if max_overlap >= 0.65:
        penalties["duplicate"] = 0.25
        reasons.append("duplicate_signal")
    try:
        activity = float(scene.get("message_rate", 0.0) or 0.0)
    except (TypeError, ValueError):
        activity = 0.0
    if activity >= 8:
        penalties["high_activity"] = 0.20
        reasons.append("high_activity")
    if atmosphere in {"emotional", "tense"}:
        penalties["sensitive_atmosphere"] = 0.30
    try:
        energy = _clamp(float(robot_state.get("energy", 1.0)))
    except (TypeError, ValueError):
        energy = 1.0
    if energy < 0.35:
        penalties["low_energy"] = 0.20
        reasons.append("low_energy")

    score = _clamp(weighted - sum(penalties.values()))
    # 没有明显社交信号时不允许靠其他因子凑出主动插话分数。
    if social_signal < 0.5:
        score = min(score, 0.49)
        reasons.append("no_social_signal")
    action = SOCIAL_ACTION if score >= _clamp(threshold) else _IGNORE_ACTION
    return SocialScore(
        score=score,
        eligible=True,
        action=action,
        factors=factors,
        penalties=penalties,
        reasons=tuple(dict.fromkeys(reasons))[:8],
    )


interject_gate = GroupInterjectGate()
