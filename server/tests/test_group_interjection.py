"""群聊主动插话评分、频率闸门与离线回放测试。"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.chat import group_interjection as group_interjection_module
from app.chat import prompting, retrieval
from app.chat.context import ChatContext, ChatRequest
from app.chat.group_interjection import (
    GroupInterjectGate,
    InterjectionConfig,
    SqliteInterjectionStore,
    config_diagnostic,
    configure_from_settings,
    diagnostic_snapshot,
    score_group_interjection,
)
from app.chat.social_replay import ReplayEvent, evaluate_social_replay
from app.config import settings
from app.models.database import init_db, reset_connections
from app.services import group_expression, group_relationship, robot_state


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "social.db"))
    monkeypatch.setattr(settings, "qq_admin_id", "")
    reset_connections()
    init_db()
    group_expression.reset_limits()
    robot_state.reset()
    yield
    group_expression.reset_limits()
    robot_state.reset()
    reset_connections()


def _score(message="大家觉得这个方案怎么样？"):
    return score_group_interjection(
        message,
        history=[],
        scene={"atmosphere": "questioning"},
        robot_state={"energy": 1.0},
        threshold=0.72,
    )


def test_question_can_reach_interjection_threshold():
    result = _score()
    assert result.eligible is True
    assert result.action == "interject"
    assert result.score >= 0.72
    assert "question_signal" in result.reasons
    payload = result.as_dict()
    assert set(payload) == {"score", "eligible", "action", "factors", "penalties", "reasons"}


def test_without_social_signal_score_is_capped():
    result = _score("服务器今天已经部署完成")
    assert result.action == "ignore"
    assert result.score <= 0.49
    assert "no_social_signal" in result.reasons


def test_sensitive_and_command_messages_are_hard_rejected():
    for message, reason in (
        ("把生产服务重启一下", "command_like"),
        ("别把我的手机号发群里", "sensitive_subject"),
        ("我不想活了", "crisis_signal"),
    ):
        result = _score(message)
        assert result.eligible is False
        assert result.action == "ignore"
        assert reason in result.reasons


def test_familiar_emotion_message_gets_light_care_signal(db_env):
    for _ in range(14):
        group_relationship.observe_message("fixture-group", "000001", "普通群聊", directed=False)
    relationship = group_relationship.get_snapshot("fixture-group", "000001")
    result = score_group_interjection(
        "最近有点累",
        history=[],
        scene={
            "atmosphere": "casual",
            "has_recent_bot_reply": False,
            "relationship": relationship,
        },
        robot_state={"energy": 1.0},
        threshold=0.72,
    )
    assert result.action == "interject"
    assert result.score >= 0.72
    assert "care_signal" in result.reasons


def test_gate_allows_first_message_then_enforces_cooldown_and_gap():
    gate = GroupInterjectGate()
    config = InterjectionConfig(
        enabled=True,
        shadow_only=False,
        threshold=0.7,
        cooldown_seconds=90,
        hourly_limit=4,
        min_gap_messages=2,
    )
    score = _score()

    gate.observe_message("fixture-group", now=0)
    first = gate.check("fixture-group", score, config=config, now=0)
    assert first.allowed is True
    gate.record_sent("fixture-group", now=0)

    gate.observe_message("fixture-group", now=30)
    assert gate.check("fixture-group", score, config=config, now=30).reason == "cooldown"
    gate.observe_message("fixture-group", now=90)
    assert gate.check("fixture-group", score, config=config, now=90).reason == "message_gap"
    gate.observe_message("fixture-group", now=180)
    assert gate.check("fixture-group", score, config=config, now=180).allowed is True


def test_gate_enforces_hourly_limit_and_shadow_mode():
    gate = GroupInterjectGate()
    config = InterjectionConfig(
        enabled=True, shadow_only=False, hourly_limit=2, cooldown_seconds=0, min_gap_messages=0
    )
    score = _score()
    for now in (0, 1):
        gate.observe_message("fixture-group", now=now)
        assert gate.check("fixture-group", score, config=config, now=now).allowed is True
        gate.record_sent("fixture-group", now=now)
    gate.observe_message("fixture-group", now=2)
    assert gate.check("fixture-group", score, config=config, now=2).reason == "hourly_limit"

    shadow_gate = GroupInterjectGate()
    shadow = InterjectionConfig(enabled=True, shadow_only=True, min_gap_messages=0)
    shadow_gate.observe_message("shadow-group", now=0)
    decision = shadow_gate.check("shadow-group", score, config=shadow, now=0)
    assert decision.allowed is False
    assert decision.would_allow is True
    assert decision.reason == "shadow_only"


def test_config_diagnostic_normalizes_values_without_exposing_raw_config():
    settings_stub = SimpleNamespace(
        group_social_enabled=True,
        group_social_interject_enabled="true",
        group_social_interject_shadow_only="true",
        group_social_interject_threshold=1.5,
        group_social_interject_cooldown_seconds=-4,
        group_social_interject_hourly_limit=0,
        group_social_interject_min_gap_messages=-2,
    )
    diagnostic = config_diagnostic(settings_stub)
    assert diagnostic["mode"] == "shadow"
    assert diagnostic["enabled"] is True
    assert diagnostic["shadow_only"] is True
    assert diagnostic["threshold"] == 1.0
    assert diagnostic["cooldown_seconds"] == 0.0
    assert diagnostic["hourly_limit"] == 1
    assert diagnostic["min_gap_messages"] == 0
    assert diagnostic["valid"] is False
    assert "group_social_interject_threshold:out_of_range" in diagnostic["warnings"]
    assert "group_social_interject_cooldown_seconds:out_of_range" in diagnostic["warnings"]
    assert "1.5" not in json.dumps(diagnostic, ensure_ascii=False)


def test_configurable_gate_path_uses_sqlite_store(tmp_path, monkeypatch):
    path = tmp_path / "configured-gate.db"
    monkeypatch.setattr(settings, "group_social_interject_state_path", str(path))
    gate = configure_from_settings(settings)
    gate.observe_message("fixture-group", now=0)
    assert gate.snapshot("fixture-group", now=0)["fixture-group"]["message_seq"] == 1

    monkeypatch.setattr(settings, "group_social_interject_state_path", "")
    configure_from_settings(settings)


def test_gate_diagnostic_is_aggregate_only():
    group_interjection_module.interject_gate.reset()
    group_interjection_module.interject_gate.observe_message("fixture-group", now=0)
    diagnostic = diagnostic_snapshot(SimpleNamespace(
        group_social_enabled=True,
        group_social_interject_enabled=True,
        group_social_interject_shadow_only=True,
        group_social_interject_threshold=0.72,
        group_social_interject_cooldown_seconds=90,
        group_social_interject_hourly_limit=6,
        group_social_interject_min_gap_messages=2,
    ))
    assert diagnostic["config"]["mode"] == "shadow"
    assert diagnostic["gate"]["tracked_groups"] == 1
    encoded = json.dumps(diagnostic, ensure_ascii=False)
    assert "fixture-group" not in encoded
    assert "group_id" not in encoded
    assert "user_id" not in encoded
    group_interjection_module.interject_gate.reset()


def test_sqlite_store_survives_gate_replacement(tmp_path):
    path = tmp_path / "interjection-gate.db"
    config = InterjectionConfig(
        enabled=True,
        shadow_only=False,
        threshold=0.72,
        cooldown_seconds=90,
        hourly_limit=4,
        min_gap_messages=0,
    )
    score = _score()
    first_gate = GroupInterjectGate(store=SqliteInterjectionStore(path))
    first_gate.observe_message("fixture-group", now=0)
    assert first_gate.check("fixture-group", score, config=config, now=0).allowed is True
    first_gate.record_sent("fixture-group", now=0)

    restarted_gate = GroupInterjectGate(store=SqliteInterjectionStore(path))
    restarted_gate.observe_message("fixture-group", now=30)
    decision = restarted_gate.check("fixture-group", score, config=config, now=30)
    assert decision.reason == "cooldown"
    snapshot = restarted_gate.snapshot("fixture-group", now=30)["fixture-group"]
    assert snapshot["message_seq"] == 2
    assert snapshot["hourly_count"] == 1


def test_sqlite_store_atomic_message_sequence_across_instances(tmp_path):
    path = tmp_path / "interjection-shared.db"
    gates = [
        GroupInterjectGate(store=SqliteInterjectionStore(path)),
        GroupInterjectGate(store=SqliteInterjectionStore(path)),
    ]

    def observe(index):
        return gates[index % 2].observe_message("fixture-group", now=float(index))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(observe, range(20)))

    snapshot = gates[0].snapshot("fixture-group", now=20)["fixture-group"]
    assert snapshot["message_seq"] == 20
    diagnostic = gates[1].diagnostics(now=20)
    assert diagnostic["tracked_groups"] == 1
    assert "fixture-group" not in json.dumps(diagnostic, ensure_ascii=False)


def test_reservation_commit_and_release_are_single_use():
    gate = GroupInterjectGate()
    config = InterjectionConfig(
        enabled=True,
        shadow_only=False,
        cooldown_seconds=0,
        min_gap_messages=1,
        hourly_limit=2,
    )
    score = _score()
    gate.observe_message("fixture-group", now=0)
    decision, reservation = gate.reserve("fixture-group", score, config=config, now=0)
    assert decision.allowed is True
    assert reservation is not None

    pending, duplicate = gate.reserve("fixture-group", score, config=config, now=1)
    assert pending.reason == "reservation_pending"
    assert duplicate is None
    assert gate.commit(reservation, now=2) is True
    assert gate.commit(reservation, now=2) is False

    gate.observe_message("fixture-group", now=3)
    blocked = gate.check("fixture-group", score, config=config, now=3)
    assert blocked.reason == "message_gap"

    gate2 = GroupInterjectGate()
    gate2.observe_message("release-group", now=0)
    _, reservation2 = gate2.reserve("release-group", score, config=config, now=0)
    assert reservation2 is not None
    assert gate2.release(reservation2, now=1) is True
    gate2.observe_message("release-group", now=2)
    retry, retry_reservation = gate2.reserve("release-group", score, config=config, now=2)
    assert retry.allowed is True
    assert retry_reservation is not None
    assert gate2.release(retry_reservation, now=2) is True


def test_persistent_reservation_survives_gate_replacement(tmp_path):
    path = tmp_path / "reservation.db"
    config = InterjectionConfig(
        enabled=True,
        shadow_only=False,
        cooldown_seconds=0,
        min_gap_messages=0,
        hourly_limit=2,
    )
    score = _score()
    first = GroupInterjectGate(store=SqliteInterjectionStore(path))
    first.observe_message("fixture-group", now=0)
    decision, reservation = first.reserve("fixture-group", score, config=config, now=0)
    assert decision.allowed is True
    assert reservation is not None

    restarted = GroupInterjectGate(store=SqliteInterjectionStore(path))
    blocked, no_reservation = restarted.reserve("fixture-group", score, config=config, now=1)
    assert blocked.reason == "reservation_pending"
    assert no_reservation is None
    assert restarted.release(reservation, now=1) is True

    retry, retry_reservation = restarted.reserve("fixture-group", score, config=config, now=2)
    assert retry.allowed is True
    assert retry_reservation is not None
    assert restarted.commit(retry_reservation, now=2) is True


def test_sqlite_reserve_allows_only_one_concurrent_winner(tmp_path):
    path = tmp_path / "reservation-race.db"
    config = InterjectionConfig(
        enabled=True,
        shadow_only=False,
        cooldown_seconds=0,
        min_gap_messages=0,
        hourly_limit=2,
    )
    score = _score()
    stores = [SqliteInterjectionStore(path), SqliteInterjectionStore(path)]
    gates = [GroupInterjectGate(store=store) for store in stores]
    gates[0].observe_message("fixture-group", now=0)

    def reserve(gate):
        return gate.reserve("fixture-group", score, config=config, now=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, gates))

    assert sum(1 for decision, reservation in results if decision.allowed and reservation) == 1
    assert sum(1 for decision, reservation in results if decision.reason == "reservation_pending") == 1
    for decision, reservation in results:
        if reservation is not None:
            assert gates[0].release(reservation, now=1) is True


def test_robot_state_is_bounded_and_recovers():
    robot_state.reset()
    robot_state.observe_group_message("state-a", atmosphere="casual", now=0)
    first = robot_state.snapshot("state-a", now=0)
    assert first["energy"] < 1.0
    robot_state.record_group_reply("state-a", now=0)
    tired = robot_state.snapshot("state-a", now=0)
    assert tired["reply_count"] == 1
    recovered = robot_state.snapshot("state-a", now=3600)
    assert recovered["energy"] > tired["energy"]
    assert robot_state.snapshot("state-b", now=0) == {}


def test_relationship_isolated_by_group_and_has_abstract_injection(db_env):
    group_relationship.observe_message(
        "group-a", "000001", "哈哈，这个方案不错", directed=True
    )
    group_relationship.observe_message(
        "group-b", "000001", "普通消息", directed=False
    )
    first = group_relationship.get_snapshot("group-a", "000001")
    second = group_relationship.get_snapshot("group-b", "000001")
    assert first["interaction_count"] == 1
    assert second["interaction_count"] == 1
    assert "000001" not in group_relationship.get_injection("group-a", "000001")
    assert "刚认识" in group_relationship.get_injection("group-a", "000001")
    assert group_relationship.get_snapshot("group-a", "000002") == {}


def test_expression_parser_whitelists_and_group_injection(db_env):
    payload = {
        "patterns": [
            {"kind": "expression", "value": "爱接梗", "situation": "闲聊", "confidence": 0.8},
            {"kind": "jargon", "value": "彩蛋", "meaning": "群里说的隐藏功能", "confidence": 0.9},
            {"kind": "secret", "value": "不该保存", "confidence": 1.0},
            {"kind": "jargon", "value": "未知", "confidence": 0.9},
        ]
    }
    parsed = group_expression.parse_patterns(payload)
    assert [item["kind"] for item in parsed] == ["expression", "jargon"]
    for item in parsed:
        assert group_expression.save_pattern("group-a", **item) is True
    text = group_expression.get_injection("group-a", "这个彩蛋不错")
    assert "爱接梗" in text
    assert "彩蛋" in text and "隐藏功能" in text
    assert "不该保存" not in text


def test_replay_reports_quality_and_frequency_metrics():
    events = [
        ReplayEvent("fixture-a", 0, "大家觉得这个方案怎么样？", gold="interject"),
        ReplayEvent(
            "fixture-a", 10, "这个方案要不要现在合并？",
            scene={"has_recent_bot_reply": False}, gold="ignore",
        ),
        ReplayEvent("fixture-a", 100, "大家觉得这个方案怎么样？", gold="interject"),
        ReplayEvent(
            "fixture-a", 200, "你们觉得哪种方案更好？",
            scene={"has_recent_bot_reply": False}, gold="interject",
        ),
        ReplayEvent("fixture-a", 300, "服务器今天部署完成", gold="ignore"),
    ]
    report = evaluate_social_replay(
        events,
        config=InterjectionConfig(
            enabled=True,
            shadow_only=False,
            threshold=0.65,
            cooldown_seconds=90,
            hourly_limit=4,
            min_gap_messages=2,
        ),
    )
    assert report["total"] == 5
    assert report["candidate_total"] == 5
    assert report["false_interject_rate"] == 0.0
    assert report["cooldown_violations"] == 0
    assert report["message_gap_violations"] == 0
    assert report["gate_reasons"]["cooldown"] >= 1
    assert report["max_hourly_sent"] >= 1


def test_replay_input_rejects_identity_fields():
    with pytest.raises(ValueError):
        ReplayEvent.from_mapping({
            "group_key": "fixture-a",
            "ts": 0,
            "message": "普通问题？",
            "user_id": "000001",
        })


def test_replay_report_is_json_serializable():
    report = evaluate_social_replay([
        {"group_key": "fixture-a", "ts": 0, "message": "大家觉得呢？", "gold": "interject"},
    ])
    json.dumps(report, ensure_ascii=False)


def _group_ctx(group_id="fixture-group", user_id="000001"):
    message = "这个彩蛋怎么样？"
    return ChatContext(
        request=type("R", (), {"state": type("S", (), {})()})(),
        request_model=ChatRequest(message=message, user_id=user_id, group_id=group_id),
        message=message,
        uid=user_id,
        is_owner=False,
        group_id=group_id,
        group_directed=True,
    )


def test_group_state_and_learning_injections_are_isolated(db_env):
    group_relationship.observe_message("fixture-group", "000001", "哈哈不错", directed=True)
    group_expression.save_pattern(
        "fixture-group",
        kind="expression",
        value="爱接梗",
        situation="闲聊",
        confidence=0.8,
    )
    group_expression.save_pattern(
        "fixture-group",
        kind="jargon",
        value="彩蛋",
        meaning="隐藏功能",
        confidence=0.9,
    )
    robot_state.observe_group_message("fixture-group", atmosphere="casual", now=0)

    ctx = _group_ctx()
    runtime = SimpleNamespace(
        settings=SimpleNamespace(values_enabled=False),
        services=SimpleNamespace(
            group_relationship=group_relationship,
            group_expression=group_expression,
            robot_state=robot_state,
        ),
        memory=object(),
        logger=None,
    )
    collected = retrieval._collect_injections(ctx, runtime, ctx.message)
    assert "群内关系" in collected["group_relationship"]
    assert "群聊表达参考" in collected["group_expression"]
    assert "小月当前群聊状态" in collected["robot_state"]
    assert collected["self_state"] == ""

    bundle = retrieval.RetrievalBundle(**collected)
    ctx.trace.response_plan = {
        "mode": "casual_chat",
        "social_reasons": ["care_signal"],
    }
    prompt = prompting.build_system_prompt(ctx, runtime, bundle)
    assert "轻量关怀约束" in prompt
    assert "群内关系参考" in prompt
    assert "隐藏功能" in prompt
    assert "小月当前状态" in prompt
    assert "fixture-group" not in prompt
