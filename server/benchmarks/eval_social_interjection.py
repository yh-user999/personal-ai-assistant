#!/usr/bin/env python3
"""用脱敏 JSONL 基准标定群聊主动插话参数。

示例：
    .venv/bin/python benchmarks/eval_social_interjection.py
    .venv/bin/python benchmarks/eval_social_interjection.py --json

只运行确定性评分/闸门，不调用 LLM，不写生产数据库。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.chat.social_replay import evaluate_social_replay, load_jsonl  # noqa: E402
from app.chat.group_interjection import InterjectionConfig  # noqa: E402

DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "group_social_baseline.jsonl"


def _grid() -> list[InterjectionConfig]:
    configs = []
    for threshold in (0.65, 0.70, 0.75):
        for cooldown in (60.0, 90.0):
            for gap in (1, 2):
                configs.append(InterjectionConfig(
                    enabled=True,
                    shadow_only=False,
                    threshold=threshold,
                    cooldown_seconds=cooldown,
                    hourly_limit=6,
                    min_gap_messages=gap,
                ))
    return configs


def _rank(report: dict) -> tuple[float, float, float]:
    """优先零违规和低误插话，再兼顾 precision/recall。"""
    violations = report["cooldown_violations"] + report["message_gap_violations"]
    safe = 1.0 if violations == 0 else 0.0
    return (
        safe,
        1.0 - float(report["false_interject_rate"]),
        0.6 * float(report["precision"]) + 0.4 * float(report["recall"]),
    )


def evaluate_fixture(events) -> dict:
    candidates = []
    for config in _grid():
        report = evaluate_social_replay(events, config=config)
        candidates.append({
            "config": report.pop("config"),
            "metrics": report,
            "rank": _rank(report),
        })
    candidates.sort(key=lambda item: item["rank"], reverse=True)
    best = candidates[0]
    return {
        "fixture_events": len(events),
        "candidate_count": len(candidates),
        "best": best,
        "safe_baseline": bool(
            best["metrics"]["cooldown_violations"] == 0
            and best["metrics"]["message_gap_violations"] == 0
            and best["metrics"]["false_interject_rate"] <= 0.2
        ),
        "candidates": candidates,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="标定群聊主动插话参数")
    parser.add_argument("--jsonl", type=Path, default=DEFAULT_FIXTURE, help="脱敏 JSONL 基准文件")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args(argv)
    result = evaluate_fixture(load_jsonl(args.jsonl))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        best = result["best"]
        print("=== 群聊主动插话基准标定 ===")
        print(f"样例 {result['fixture_events']} 条，候选参数 {result['candidate_count']} 组")
        print(f"推荐配置：{json.dumps(best['config'], ensure_ascii=False)}")
        print(f"precision={best['metrics']['precision']} recall={best['metrics']['recall']} ")
        print(
            "false_interject_rate="
            f"{best['metrics']['false_interject_rate']} "
            f"silence_rate={best['metrics']['silence_rate']} "
            f"violations={best['metrics']['cooldown_violations'] + best['metrics']['message_gap_violations']}"
        )
        print("safe_baseline=" + str(result["safe_baseline"]))
    return 0 if result["safe_baseline"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
