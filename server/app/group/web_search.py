"""群聊联网检索的有界限流状态。

本模块只保存按群隔离的时间窗口元数据，不保存消息正文、搜索词或搜索结果。
具体的搜索与来源处理仍由聊天检索层负责，避免群领域包反向依赖聊天编排。
"""
from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Deque


@dataclass
class _GroupBucket:
    started: Deque[float] = field(default_factory=deque)
    pending: int = 0
    last_started: float | None = None
    last_touched: float = 0.0


@dataclass(frozen=True)
class ReservationDecision:
    allowed: bool
    reason: str
    reservation: "GroupWebSearchReservation | None" = None
    remaining: int = 0


class GroupWebSearchReservation:
    """一次群搜索预留；commit/release 都是幂等的。"""

    def __init__(self, limiter: "GroupWebSearchLimiter", group_id: str, token: int):
        self._limiter = limiter
        self.group_id = group_id
        self._token = token
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def commit(self, *, now: float | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        self._limiter._commit(self.group_id, self._token, now=now)

    def release(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._limiter._release(self.group_id, self._token)


class GroupWebSearchLimiter:
    """按群限制搜索频率，并为并发请求提供有界 reservation。"""

    def __init__(self, *, window_seconds: float = 3600.0, max_groups: int = 256):
        self.window_seconds = max(1.0, float(window_seconds))
        self.max_groups = max(1, int(max_groups))
        self._buckets: OrderedDict[str, _GroupBucket] = OrderedDict()
        self._reservations: dict[int, str] = {}
        self._next_token = 0

    def reset(self) -> None:
        self._buckets.clear()
        self._reservations.clear()
        self._next_token = 0

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        for bucket in self._buckets.values():
            while bucket.started and bucket.started[0] <= cutoff:
                bucket.started.popleft()
        stale = [
            group_id
            for group_id, bucket in self._buckets.items()
            if bucket.pending == 0
            and not bucket.started
            and (bucket.last_touched <= cutoff or bucket.last_touched == 0)
        ]
        for group_id in stale:
            self._buckets.pop(group_id, None)

    def _bucket_for(self, group_id: str, now: float) -> _GroupBucket | None:
        bucket = self._buckets.get(group_id)
        if bucket is not None:
            self._buckets.move_to_end(group_id)
            bucket.last_touched = now
            return bucket
        if len(self._buckets) >= self.max_groups:
            idle = next(
                (
                    key
                    for key, candidate in self._buckets.items()
                    if candidate.pending == 0
                ),
                None,
            )
            if idle is None:
                return None
            self._buckets.pop(idle, None)
        bucket = _GroupBucket(last_touched=now)
        self._buckets[group_id] = bucket
        return bucket

    def reserve(
        self,
        group_id: str,
        *,
        hourly_limit: int = 3,
        cooldown_seconds: float = 15.0,
        now: float | None = None,
    ) -> ReservationDecision:
        group_id = str(group_id or "").strip()
        if not group_id:
            return ReservationDecision(False, "invalid_group")
        now = time.monotonic() if now is None else float(now)
        hourly_limit = max(1, int(hourly_limit))
        cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._prune(now)
        bucket = self._bucket_for(group_id, now)
        if bucket is None:
            return ReservationDecision(False, "capacity")
        if bucket.last_started is not None and now - bucket.last_started < cooldown_seconds:
            return ReservationDecision(False, "cooldown", remaining=max(0, hourly_limit - len(bucket.started)))
        if len(bucket.started) + bucket.pending >= hourly_limit:
            return ReservationDecision(False, "hourly_limit", remaining=0)

        self._next_token += 1
        token = self._next_token
        bucket.pending += 1
        bucket.last_started = now
        bucket.last_touched = now
        self._reservations[token] = group_id
        remaining = max(0, hourly_limit - len(bucket.started) - bucket.pending)
        return ReservationDecision(
            True,
            "reserved",
            reservation=GroupWebSearchReservation(self, group_id, token),
            remaining=remaining,
        )

    def _commit(self, group_id: str, token: int, *, now: float | None = None) -> None:
        if self._reservations.pop(token, None) != group_id:
            return
        bucket = self._buckets.get(group_id)
        if bucket is None:
            return
        bucket.pending = max(0, bucket.pending - 1)
        bucket.started.append(time.monotonic() if now is None else float(now))
        bucket.last_touched = time.monotonic() if now is None else float(now)

    def _release(self, group_id: str, token: int) -> None:
        if self._reservations.pop(token, None) != group_id:
            return
        bucket = self._buckets.get(group_id)
        if bucket is not None:
            bucket.pending = max(0, bucket.pending - 1)
            # 失败/取消的预留不应制造冷却；若还有已提交请求，
            # 冷却基准回到最近一次真实成功搜索。
            bucket.last_started = bucket.started[-1] if bucket.started else None
            bucket.last_touched = time.monotonic()

    def snapshot(self, group_id: str, *, now: float | None = None) -> dict[str, int | float]:
        now = time.monotonic() if now is None else float(now)
        self._prune(now)
        bucket = self._buckets.get(str(group_id or "").strip())
        if bucket is None:
            return {"started": 0, "pending": 0, "tracked_groups": len(self._buckets)}
        return {
            "started": len(bucket.started),
            "pending": bucket.pending,
            "tracked_groups": len(self._buckets),
        }


limiter = GroupWebSearchLimiter()
