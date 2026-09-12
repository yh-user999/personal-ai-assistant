"""群聊主动插话离线回放。

输入是脱敏 JSONL fixture，评估只运行确定性评分器和频率闸门，不调用 LLM、
不写生产数据库。"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from app.chat.group_interjection import (
    GroupInterjectGate,
    InterjectionConfig,
    score_group_interjection,
)

_ALLOWED_EVENT_KEYS = frozenset({"group_key", "ts", "message", "directed", "scene", "gold"})
_ALLOWED_GOLDS = frozenset({"interject", "ignore"})


@dataclass(frozen=True)
class ReplayEvent:
    group_key: str
    ts: float
    message: str
    directed: bool = False
    scene: dict[str, Any] = field(default_factory=dict)
    gold: str = "ignore"

    @classmethod
    def from_mapping(cls, raw: object) -> "ReplayEvent":
        if not isinstance(raw, dict):
            raise ValueError("回放行必须是 JSON 对象")
        forbidden = set(raw) - _ALLOWED_EVENT_KEYS
        if forbidden:
            raise ValueError("回放输入含未允许字段")
        group_key = str(raw.get("group_key") or "").strip()
        if not group_key or len(group_key) > 64:
            raise ValueError("group_key 不能为空且不得超过 64 字符")
        if "user_id" in raw or "group_id" in raw:
            raise ValueError("回放只接受脱敏 group_key，不接受身份字段")
        try:
            ts = float(raw.get("ts"))
        except (TypeError, ValueError):
            raise ValueError("ts 必须是数字") from None
        message = " ".join(str(raw.get("message") or "").split())
        if not message or len(message) > 800:
            raise ValueError("message 不能为空且不得超过 800 字符")
        scene = raw.get("scene") or {}
        if not isinstance(scene, dict):
            raise ValueError("scene 必须是对象")
        safe_scene = {
            str(key)[:40]: value
            for key, value in scene.items()
            if str(key) in {"atmosphere", "topic_shift", "message_rate", "has_recent_bot_reply", "energy"}
        }
        gold = str(raw.get("gold") or "ignore").strip().lower()
        if gold not in _ALLOWED_GOLDS:
            raise ValueError("gold 只能是 interject 或 ignore")
        return cls(
            group_key=group_key,
            ts=ts,
            message=message,
            directed=bool(raw.get("directed", False)),
            scene=safe_scene,
            gold=gold,
        )


def load_jsonl(path: str | Path) -> list[ReplayEvent]:
    """读取脱敏 JSONL；空行跳过，错误包含行号。"""
    events: list[ReplayEvent] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                events.append(ReplayEvent.from_mapping(json.loads(line)))
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"回放第 {line_no} 行无效：{exc}") from exc
    return events


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def evaluate_social_replay(
    events: Iterable[ReplayEvent | dict[str, Any]],
    *,
    config: InterjectionConfig | None = None,
) -> dict[str, Any]:
    """重放评分/闸门并返回可序列化指标。"""
    cfg = (config or InterjectionConfig(enabled=True, shadow_only=False)).normalized()
    # shadow 只影响线上是否执行，离线评估统计 should_allow，避免把 shadow 全算成负例。
    eval_config = InterjectionConfig(
        enabled=True,
        shadow_only=False,
        threshold=cfg.threshold,
        cooldown_seconds=cfg.cooldown_seconds,
        hourly_limit=cfg.hourly_limit,
        min_gap_messages=cfg.min_gap_messages,
        max_groups=cfg.max_groups,
    )
    parsed = [item if isinstance(item, ReplayEvent) else ReplayEvent.from_mapping(item) for item in events]
    ordered = sorted(enumerate(parsed), key=lambda pair: (pair[1].ts, pair[0]))
    gate = GroupInterjectGate(max_groups=eval_config.max_groups)
    history: dict[str, list[dict[str, str]]] = defaultdict(list)
    stats = Counter()
    gate_reasons: Counter[str] = Counter()
    atmosphere_counts: Counter[str] = Counter()
    sent_times: dict[str, list[float]] = defaultdict(list)
    sent_sequences: dict[str, list[int]] = defaultdict(list)
    max_hourly_sent = 0

    for _index, event in ordered:
        current_history = history[event.group_key][-8:]
        scene = dict(event.scene)
        if "has_recent_bot_reply" not in scene:
            scene["has_recent_bot_reply"] = any(
                item.get("role") == "assistant" for item in current_history[-2:]
            )
        gate.observe_message(event.group_key, now=event.ts)
        score = score_group_interjection(
            event.message,
            current_history,
            scene,
            {"energy": scene.get("energy", 1.0)},
            directed=event.directed,
            threshold=eval_config.threshold,
        )
        decision = gate.check(event.group_key, score, config=eval_config, now=event.ts)
        predicted = bool(decision.would_allow and not event.directed)
        gold = event.gold == "interject" and not event.directed
        stats["total"] += 1
        if not event.directed:
            stats["candidate_total"] += 1
            stats["predicted_interject" if predicted else "predicted_ignore"] += 1
            stats["gold_interject" if gold else "gold_ignore"] += 1
            if predicted and gold:
                stats["true_positive"] += 1
            elif predicted and not gold:
                stats["false_positive"] += 1
            elif not predicted and gold:
                stats["false_negative"] += 1
        gate_reasons[decision.reason] += 1
        atmosphere_counts[str(scene.get("atmosphere") or "casual")[:32]] += 1

        history[event.group_key].append({"role": "user", "content": event.message})
        if predicted:
            gate.record_sent(event.group_key, now=event.ts)
            sent_times[event.group_key].append(event.ts)
            history[event.group_key].append({"role": "assistant", "content": "[interject]"})
        history[event.group_key] = history[event.group_key][-8:]
        snapshot = gate.snapshot(event.group_key, now=event.ts)
        group_snapshot = snapshot[event.group_key]
        max_hourly_sent = max(max_hourly_sent, int(group_snapshot["hourly_count"]))
        if predicted:
            sent_sequences[event.group_key].append(int(group_snapshot["message_seq"]))

    candidate_total = stats["candidate_total"]
    tp = stats["true_positive"]
    fp = stats["false_positive"]
    fn = stats["false_negative"]
    cooldown_violations = 0
    message_gap_violations = 0
    for group_times in sent_times.values():
        for previous, current in zip(group_times, group_times[1:]):
            if current - previous < eval_config.cooldown_seconds:
                cooldown_violations += 1
    for group_sequences in sent_sequences.values():
        for previous, current in zip(group_sequences, group_sequences[1:]):
            if current - previous - 1 < eval_config.min_gap_messages:
                message_gap_violations += 1
    return {
        "total": stats["total"],
        "candidate_total": candidate_total,
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
        "false_interject_rate": _ratio(fp, stats["predicted_interject"]),
        "silence_rate": _ratio(stats["predicted_ignore"], candidate_total),
        "max_hourly_sent": max_hourly_sent,
        "cooldown_violations": cooldown_violations,
        "message_gap_violations": message_gap_violations,
        "gate_reasons": dict(gate_reasons),
        "atmosphere_counts": dict(atmosphere_counts),
        "config": {
            "threshold": eval_config.threshold,
            "cooldown_seconds": eval_config.cooldown_seconds,
            "hourly_limit": eval_config.hourly_limit,
            "min_gap_messages": eval_config.min_gap_messages,
        },
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="评估群聊主动插话评分与频率闸门")
    parser.add_argument("jsonl", help="脱敏 JSONL 回放文件")
    args = parser.parse_args(argv)
    print(json.dumps(evaluate_social_replay(load_jsonl(args.jsonl)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
