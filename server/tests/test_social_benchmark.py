"""主动插话合成基准的确定性验收。"""
from __future__ import annotations

import json
from pathlib import Path

from benchmarks.eval_interjection_process import evaluate_process_contention
from benchmarks.eval_social_interjection import DEFAULT_FIXTURE, evaluate_fixture
from benchmarks.social_replay import load_jsonl

PROCESS_BASELINE = Path(__file__).resolve().parent / "fixtures" / "group_interjection_process_baseline.json"


def test_synthetic_social_baseline_is_safe_and_reproducible():
    events = load_jsonl(DEFAULT_FIXTURE)
    result = evaluate_fixture(events)
    assert len(events) == 20
    assert result["safe_baseline"] is True
    assert result["best"]["metrics"]["cooldown_violations"] == 0
    assert result["best"]["metrics"]["message_gap_violations"] == 0
    assert result["best"]["metrics"]["false_interject_rate"] <= 0.2
    encoded = json.dumps(result, ensure_ascii=False)
    assert "user_id" not in encoded
    assert "group_id" not in encoded


def test_independent_process_reservation_baseline_is_safe(tmp_path):
    baseline = json.loads(PROCESS_BASELINE.read_text(encoding="utf-8"))
    report = evaluate_process_contention(
        tmp_path / "process-baseline.db",
        process_count=baseline["process_count"],
    )
    assert report["safe_baseline"] is True
    assert report["attempts"] == baseline["attempts"]
    assert report["reservations"] == baseline["expected"]["reservations"]
    assert report["commits"] == baseline["expected"]["commits"]
    assert report["final_snapshot"] == {
        "message_seq": baseline["expected"]["message_seq"],
        "hourly_count": baseline["expected"]["hourly_count"],
        "pending_count": baseline["expected"]["pending_count"],
    }
    encoded = json.dumps(report, ensure_ascii=False)
    assert "user_id" not in encoded
    assert "group_id" not in encoded
