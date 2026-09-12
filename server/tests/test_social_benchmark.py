"""主动插话合成基准的确定性验收。"""
from __future__ import annotations

import json

from app.chat.social_replay import load_jsonl
from benchmarks.eval_social_interjection import DEFAULT_FIXTURE, evaluate_fixture


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
