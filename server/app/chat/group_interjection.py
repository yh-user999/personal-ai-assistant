"""群聊主动插话的确定性评分与频率闸门。

本模块不调用 LLM、不写数据库、不保存原始消息。评分结果只决定“是否值得
抢话”，事实回答、工具调用、权限和安全策略仍由现有聊天流水线负责。
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Protocol

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


class InterjectionStateStore(Protocol):
    """主动插话闸门的持久化替换接口。"""

    def update(
        self,
        group_id: str,
        updater: Callable[[_GroupGateState], None],
        *,
        now: float | None = None,
    ) -> _GroupGateState: ...

    def read(self, group_id: str, *, now: float | None = None) -> _GroupGateState: ...

    def snapshot(self, group_id: str | None = None, *, now: float | None = None) -> dict[str, Any]: ...

    def diagnostics(self, *, now: float | None = None) -> dict[str, Any]: ...

    def reset(self, group_id: str | None = None) -> None: ...


class SqliteInterjectionStore:
    """跨进程/重启可恢复的 SQLite 闸门状态存储。

    每次 update 使用 ``BEGIN IMMEDIATE``，只保存计数、时间和消息序号；不保存
    消息正文、用户 ID 或 prompt。GroupInterjectGate 通过构造注入使用它，默认
    仍不改变现有进程内路径。
    """

    def __init__(self, path: str | Path, *, max_groups: int = 256) -> None:
        self.path = str(Path(path).expanduser())
        self.max_groups = max(8, int(max_groups))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _initialize(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS group_interjection_state ("
                "group_key TEXT PRIMARY KEY, message_seq INTEGER NOT NULL DEFAULT 0, "
                "last_sent_at REAL, last_sent_seq INTEGER, sent_at TEXT NOT NULL DEFAULT '[]', "
                "last_seen_at REAL NOT NULL DEFAULT 0)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_group_interjection_seen "
                "ON group_interjection_state(last_seen_at DESC)"
            )
        finally:
            conn.close()

    @staticmethod
    def _group(group_id: str) -> str:
        group = str(group_id or "").strip()
        if not group:
            raise ValueError("group_id is required")
        return group

    @staticmethod
    def _from_row(row: sqlite3.Row | None, current: float) -> _GroupGateState:
        if row is None:
            return _GroupGateState(last_seen_at=current)
        try:
            raw_sent = json.loads(row["sent_at"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_sent = []
        sent_at = deque()
        for value in raw_sent if isinstance(raw_sent, list) else []:
            try:
                number = float(value)
                if math.isfinite(number):
                    sent_at.append(number)
            except (TypeError, ValueError):
                continue
        return _GroupGateState(
            message_seq=max(0, int(row["message_seq"] or 0)),
            last_sent_at=(float(row["last_sent_at"]) if row["last_sent_at"] is not None else None),
            last_sent_seq=(int(row["last_sent_seq"]) if row["last_sent_seq"] is not None else None),
            sent_at=sent_at,
            last_seen_at=float(row["last_seen_at"] or current),
        )

    @staticmethod
    def _write(conn: sqlite3.Connection, group: str, state: _GroupGateState) -> None:
        conn.execute("DELETE FROM group_interjection_state WHERE group_key=?", (group,))
        conn.execute(
            "INSERT INTO group_interjection_state "
            "(group_key, message_seq, last_sent_at, last_sent_seq, sent_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                group,
                max(0, int(state.message_seq)),
                state.last_sent_at,
                state.last_sent_seq,
                json.dumps(list(state.sent_at), separators=(",", ":")),
                state.last_seen_at,
            ),
        )

    def update(
        self,
        group_id: str,
        updater: Callable[[_GroupGateState], None],
        *,
        now: float | None = None,
    ) -> _GroupGateState:
        group = self._group(group_id)
        current = _now(now)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT message_seq, last_sent_at, last_sent_seq, sent_at, last_seen_at "
                "FROM group_interjection_state WHERE group_key=?",
                (group,),
            ).fetchone()
            state = self._from_row(row, current)
            state.last_seen_at = current
            updater(state)
            GroupInterjectGate._prune_sent(state, current)
            self._write(conn, group, state)
            conn.execute(
                "DELETE FROM group_interjection_state WHERE group_key NOT IN ("
                "SELECT group_key FROM group_interjection_state "
                "ORDER BY last_seen_at DESC LIMIT ?)",
                (self.max_groups,),
            )
            conn.commit()
            return state
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def read(self, group_id: str, *, now: float | None = None) -> _GroupGateState:
        group = self._group(group_id)
        current = _now(now)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT message_seq, last_sent_at, last_sent_seq, sent_at, last_seen_at "
                "FROM group_interjection_state WHERE group_key=?",
                (group,),
            ).fetchone()
            state = self._from_row(row, current)
            GroupInterjectGate._prune_sent(state, current)
            return state
        finally:
            conn.close()

    def snapshot(self, group_id: str | None = None, *, now: float | None = None) -> dict[str, Any]:
        current = _now(now)
        conn = self._connect()
        try:
            if group_id:
                rows = conn.execute(
                    "SELECT group_key, message_seq, last_sent_at, last_sent_seq, sent_at, last_seen_at "
                    "FROM group_interjection_state WHERE group_key=?",
                    (self._group(group_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT group_key, message_seq, last_sent_at, last_sent_seq, sent_at, last_seen_at "
                    "FROM group_interjection_state"
                ).fetchall()
            output: dict[str, Any] = {}
            for row in rows:
                state = self._from_row(row, current)
                GroupInterjectGate._prune_sent(state, current)
                output[str(row["group_key"])] = {
                    "message_seq": state.message_seq,
                    "last_sent_at": state.last_sent_at,
                    "last_sent_seq": state.last_sent_seq,
                    "hourly_count": len(state.sent_at),
                }
            return output
        finally:
            conn.close()

    def diagnostics(self, *, now: float | None = None) -> dict[str, Any]:
        snapshot = self.snapshot(now=now)
        hourly_counts = [int(item["hourly_count"]) for item in snapshot.values()]
        message_sequences = [int(item["message_seq"]) for item in snapshot.values()]
        return {
            "tracked_groups": len(snapshot),
            "groups_with_sent": sum(1 for count in hourly_counts if count > 0),
            "hourly_sent_total": sum(hourly_counts),
            "max_message_seq": max(message_sequences, default=0),
        }

    def reset(self, group_id: str | None = None) -> None:
        conn = self._connect()
        try:
            if group_id:
                conn.execute(
                    "DELETE FROM group_interjection_state WHERE group_key=?",
                    (self._group(group_id),),
                )
            else:
                conn.execute("DELETE FROM group_interjection_state")
        finally:
            conn.close()


class GroupInterjectGate:
    """按群隔离的频率闸门，可通过 store 替换为持久化实现。

    默认使用进程内状态；注入 InterjectionStateStore 后，状态读取和更新走外部
    存储。SqliteInterjectionStore 使用 SQLite 原子事务，适合重启恢复和多进程共享。
    """

    def __init__(
        self,
        *,
        max_groups: int = 256,
        store: InterjectionStateStore | None = None,
    ) -> None:
        self._states: OrderedDict[str, _GroupGateState] = OrderedDict()
        self._max_groups = max(8, int(max_groups))
        self._store = store
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
        if self._store is not None:
            state = self._store.update(
                group_id,
                lambda item: setattr(item, "message_seq", item.message_seq + 1),
                now=current,
            )
            return state.message_seq
        with self._lock:
            state = self._state(group_id, current)
            state.message_seq += 1
            self._prune_sent(state, current)
            return state.message_seq

    @staticmethod
    def _decide(
        state: _GroupGateState,
        score: SocialScore,
        cfg: InterjectionConfig,
        current: float,
    ) -> GateDecision:
        GroupInterjectGate._prune_sent(state, current)
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
        if self._store is not None:
            try:
                state = self._store.read(group_id, now=current)
            except ValueError:
                return GateDecision(False, False, "group_missing")
            return self._decide(state, score, cfg, current)
        with self._lock:
            try:
                state = self._state(group_id, current)
            except ValueError:
                return GateDecision(False, False, "group_missing")
            return self._decide(state, score, cfg, current)

    def record_sent(self, group_id: str, *, now: float | None = None) -> None:
        current = _now(now)
        if self._store is not None:
            def mark_sent(state: _GroupGateState) -> None:
                state.last_sent_at = current
                state.last_sent_seq = state.message_seq
                state.sent_at.append(current)

            self._store.update(group_id, mark_sent, now=current)
            return
        with self._lock:
            state = self._state(group_id, current)
            self._prune_sent(state, current)
            state.last_sent_at = current
            state.last_sent_seq = state.message_seq
            state.sent_at.append(current)

    def reset(self, group_id: str | None = None) -> None:
        if self._store is not None:
            self._store.reset(group_id)
            return
        with self._lock:
            if group_id is None:
                self._states.clear()
            else:
                self._states.pop(str(group_id).strip(), None)

    def snapshot(self, group_id: str | None = None, *, now: float | None = None) -> dict[str, Any]:
        if self._store is not None:
            return self._store.snapshot(group_id, now=now)
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

    def diagnostics(self, *, now: float | None = None) -> dict[str, Any]:
        """只返回聚合闸门状态，不暴露群键、消息或用户身份。"""
        if self._store is not None:
            return self._store.diagnostics(now=now)
        snapshot = self.snapshot(now=now)
        hourly_counts = [int(item["hourly_count"]) for item in snapshot.values()]
        message_sequences = [int(item["message_seq"]) for item in snapshot.values()]
        return {
            "tracked_groups": len(snapshot),
            "groups_with_sent": sum(1 for count in hourly_counts if count > 0),
            "hourly_sent_total": sum(hourly_counts),
            "max_message_seq": max(message_sequences, default=0),
        }


def _now(value: float | None) -> float:
    import time

    return time.time() if value is None else float(value)


def _coerce_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if value is None:
        return default
    raise ValueError("invalid boolean")


def config_from_settings(settings: Any) -> InterjectionConfig:
    """从 Settings/测试替身读取参数，并对非法配置安全收敛。"""
    try:
        social_enabled = _coerce_bool(getattr(settings, "group_social_enabled", True), True)
        interject_enabled = _coerce_bool(
            getattr(settings, "group_social_interject_enabled", False), False
        )
        values = {
            "enabled": social_enabled and interject_enabled,
            "shadow_only": _coerce_bool(
                getattr(settings, "group_social_interject_shadow_only", True), True
            ),
            "threshold": getattr(settings, "group_social_interject_threshold", 0.72),
            "cooldown_seconds": getattr(settings, "group_social_interject_cooldown_seconds", 90.0),
            "hourly_limit": getattr(settings, "group_social_interject_hourly_limit", 6),
            "min_gap_messages": getattr(settings, "group_social_interject_min_gap_messages", 2),
        }
        return InterjectionConfig(**values).normalized()
    except (TypeError, ValueError):
        return InterjectionConfig().normalized()


def config_diagnostic(settings: Any) -> dict[str, Any]:
    """返回不含原始配置值的有效配置诊断。"""
    warnings: list[str] = []
    bool_fields = {
        "group_social_enabled": True,
        "group_social_interject_enabled": False,
        "group_social_interject_shadow_only": True,
    }
    for name, default in bool_fields.items():
        try:
            _coerce_bool(getattr(settings, name, default), default)
        except (TypeError, ValueError):
            warnings.append(f"{name}:invalid")

    numeric_fields = (
        ("group_social_interject_threshold", 0.0, 1.0, float),
        ("group_social_interject_cooldown_seconds", 0.0, None, float),
        ("group_social_interject_hourly_limit", 1.0, None, int),
        ("group_social_interject_min_gap_messages", 0.0, None, int),
    )
    for name, minimum, maximum, converter in numeric_fields:
        raw = getattr(settings, name, None)
        try:
            value = converter(raw)
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError
            if value < minimum or (maximum is not None and value > maximum):
                warnings.append(f"{name}:out_of_range")
        except (TypeError, ValueError, OverflowError):
            warnings.append(f"{name}:invalid")

    config = config_from_settings(settings)
    mode = "disabled"
    if config.enabled:
        mode = "shadow" if config.shadow_only else "active"
    return {
        "schema_version": 1,
        "mode": mode,
        "enabled": config.enabled,
        "shadow_only": config.shadow_only,
        "threshold": config.threshold,
        "cooldown_seconds": config.cooldown_seconds,
        "hourly_limit": config.hourly_limit,
        "min_gap_messages": config.min_gap_messages,
        "max_groups": config.max_groups,
        "valid": not warnings,
        "warnings": sorted(set(warnings)),
    }


def diagnostic_snapshot(settings: Any) -> dict[str, Any]:
    """组合有效配置和聚合闸门状态，供 owner/internal 只读诊断。"""
    return {
        "schema_version": 1,
        "config": config_diagnostic(settings),
        "gate": interject_gate.diagnostics(),
    }


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
    relationship = scene.get("relationship") or {}
    try:
        familiarity = _clamp(float(relationship.get("familiarity", 0.0)))
        affinity = max(-1.0, min(1.0, float(relationship.get("affinity", 0.0))))
    except (AttributeError, TypeError, ValueError):
        familiarity, affinity = 0.0, 0.0
    care_signal = bool(
        _EMOTION_RE.search(text)
        and not has_recent_bot
        and familiarity >= 0.2
        and affinity >= -0.15
    )
    if care_signal:
        social_signal = max(social_signal, 0.9)
        reasons.append("care_signal")
    conversation_gap = 0.15 if has_recent_bot else 0.85
    if not has_recent_bot:
        reasons.append("bot_gap")
    else:
        reasons.append("recent_bot_reply")

    atmosphere = "emotional" if care_signal else str(
        scene.get("atmosphere") or "casual"
    ).strip().lower()
    scene_fit = {
        "celebration": 0.95,
        "casual": 0.85,
        "questioning": 0.80,
        "technical": 0.65,
        "emotional": 0.72 if care_signal else 0.35,
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
        penalties["sensitive_atmosphere"] = 0.05 if care_signal else 0.30
    try:
        energy = _clamp(float(robot_state.get("energy", 1.0)))
    except (TypeError, ValueError):
        energy = 1.0
    if energy < 0.35:
        penalties["low_energy"] = 0.20
        reasons.append("low_energy")

    score = _clamp(weighted - sum(penalties.values()))
    if care_signal:
        # 轻量关怀允许比普通闲聊稍积极，但仍由冷却/预算/安全门禁兜底。
        score = _clamp(score + 0.12)
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
