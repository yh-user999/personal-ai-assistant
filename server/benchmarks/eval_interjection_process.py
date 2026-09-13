#!/usr/bin/env python3
"""独立 OS 进程竞争主动插话 reservation 的脱敏基线。

示例：
    .venv/bin/python benchmarks/eval_interjection_process.py
    .venv/bin/python benchmarks/eval_interjection_process.py --processes 8 --json

只使用临时/指定 SQLite 状态文件和合成群键，不调用 LLM，不写生产数据库。
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.chat.group_interjection import (  # noqa: E402
    GroupInterjectGate,
    InterjectionConfig,
    SocialScore,
    SqliteInterjectionStore,
)

GROUP_KEY = "fixture-group"


def _score() -> SocialScore:
    return SocialScore(
        score=0.95,
        eligible=True,
        action="interject",
        reasons=("process_fixture",),
    )


def _config() -> InterjectionConfig:
    return InterjectionConfig(
        enabled=True,
        shadow_only=False,
        threshold=0.7,
        cooldown_seconds=0,
        hourly_limit=1,
        min_gap_messages=0,
        reservation_seconds=30,
    )


def _reserve_and_commit(args: tuple[str, int]) -> dict[str, Any]:
    db_path, worker_index = args
    gate = GroupInterjectGate(store=SqliteInterjectionStore(db_path))
    decision, reservation = gate.reserve(
        GROUP_KEY,
        _score(),
        config=_config(),
        now=1.0,
    )
    committed = False
    if reservation is not None:
        committed = gate.commit(reservation, now=2.0)
    return {
        "worker": worker_index,
        "allowed": bool(decision.allowed),
        "would_allow": bool(decision.would_allow),
        "reason": decision.reason,
        "reserved": reservation is not None,
        "committed": committed,
    }


def evaluate_process_contention(db_path: str | Path, *, process_count: int = 4) -> dict[str, Any]:
    """用独立 spawn 进程竞争一个名额，并返回可审计聚合指标。"""
    process_count = max(2, min(32, int(process_count)))
    path = str(db_path)
    seed_gate = GroupInterjectGate(store=SqliteInterjectionStore(path))
    seed_gate.observe_message(GROUP_KEY, now=0.0)

    context = mp.get_context("spawn")
    with context.Pool(processes=process_count, maxtasksperchild=1) as pool:
        results = pool.map(
            _reserve_and_commit,
            [(path, index) for index in range(process_count)],
        )

    final_gate = GroupInterjectGate(store=SqliteInterjectionStore(path))
    snapshot = final_gate.snapshot(GROUP_KEY, now=2.0).get(GROUP_KEY, {})
    commits = sum(1 for item in results if item["committed"])
    reservations = sum(1 for item in results if item["reserved"])
    pending_denials = sum(1 for item in results if item["reason"] == "reservation_pending")
    hourly_denials = sum(1 for item in results if item["reason"] == "hourly_limit")
    safe_baseline = bool(
        commits == 1
        and reservations == 1
        and snapshot.get("hourly_count") == 1
        and snapshot.get("pending_count", 0) == 0
    )
    return {
        "process_count": process_count,
        "attempts": len(results),
        "reservations": reservations,
        "commits": commits,
        "pending_denials": pending_denials,
        "hourly_limit_denials": hourly_denials,
        "final_snapshot": {
            "message_seq": int(snapshot.get("message_seq", 0)),
            "hourly_count": int(snapshot.get("hourly_count", 0)),
            "pending_count": int(snapshot.get("pending_count", 0)),
        },
        "safe_baseline": safe_baseline,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行独立进程 reservation 基线")
    parser.add_argument("--processes", type=int, default=4, help="竞争进程数，范围 2-32")
    parser.add_argument("--db", type=Path, default=None, help="可选 SQLite 状态文件")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args(argv)

    if args.db is not None:
        report = evaluate_process_contention(args.db, process_count=args.processes)
        return _print_report(report, as_json=args.json)

    with tempfile.TemporaryDirectory(prefix="interjection-baseline-") as temp_dir:
        report = evaluate_process_contention(
            Path(temp_dir) / "gate.db",
            process_count=args.processes,
        )
    return _print_report(report, as_json=args.json)


def _print_report(report: dict[str, Any], *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("=== 独立进程主动插话 reservation 基线 ===")
        print(f"进程数={report['process_count']} 尝试={report['attempts']}")
        print(
            f"reservations={report['reservations']} commits={report['commits']} "
            f"pending_denials={report['pending_denials']} "
            f"hourly_limit_denials={report['hourly_limit_denials']}"
        )
        print(f"final_snapshot={json.dumps(report['final_snapshot'], ensure_ascii=False)}")
        print("safe_baseline=" + str(report["safe_baseline"]))
    return 0 if report["safe_baseline"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
