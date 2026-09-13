"""群聊心流状态与发言节奏测试。"""
from __future__ import annotations

from app.chat import heartflow


def setup_function():
    heartflow.reset()


def teardown_function():
    heartflow.reset()


def test_directed_message_always_has_answer_path():
    config = heartflow.HeartflowConfig(enabled=True, talk_frequency=0.0)
    heartflow.observe_message("flow-group", directed=True, now=1000)

    result = heartflow.decide(
        "flow-group", score=0.0, directed=True, config=config, now=1000
    )

    assert result.allowed is True
    assert result.action == "answer"
    assert result.reason == "directed"


def test_zero_frequency_keeps_non_directed_messages_silent():
    config = heartflow.HeartflowConfig(enabled=True, talk_frequency=0.0)
    heartflow.observe_message("flow-group", directed=False, now=1000)

    result = heartflow.decide(
        "flow-group", score=1.0, directed=False, config=config, now=1000
    )

    assert result.allowed is False
    assert result.reason == "frequency_disabled"


def test_frequency_and_energy_control_autonomous_reply():
    config = heartflow.HeartflowConfig(
        enabled=True,
        talk_frequency=1.0,
        silence_seconds=0.0,
        max_consecutive_replies=2,
    )
    heartflow.observe_message("flow-group", directed=False, now=1000)
    result = heartflow.decide(
        "flow-group", score=0.95, directed=False, config=config, now=1000
    )

    assert result.allowed is True
    assert result.reason == "heartflow_ready"
    heartflow.record_reply("flow-group", action="interject", now=1000)
    assert heartflow.snapshot("flow-group", now=1000)["reply_count"] == 1


def test_silence_window_and_consecutive_limit_are_enforced():
    config = heartflow.HeartflowConfig(
        enabled=True,
        talk_frequency=1.0,
        silence_seconds=60.0,
        max_consecutive_replies=1,
    )
    heartflow.observe_message("flow-group", directed=False, now=1000)
    heartflow.record_reply("flow-group", action="interject", now=1000)

    assert heartflow.decide(
        "flow-group", score=1.0, directed=False, config=config, now=1010
    ).reason == "silence_window"
    assert heartflow.decide(
        "flow-group", score=1.0, directed=False, config=config, now=1100
    ).reason == "consecutive_reply_limit"


def test_idle_time_recovers_energy_without_exposing_raw_messages():
    heartflow.observe_message("flow-group", directed=True, now=1000)
    before = heartflow.snapshot("flow-group", now=1000)["energy"]
    after = heartflow.snapshot("flow-group", now=1000 + 60 * 10)["energy"]

    assert after > before
    assert "content" not in heartflow.snapshot("flow-group")
