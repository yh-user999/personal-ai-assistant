"""群聊事件驱动轻量关怀测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.group import care as group_care
from app.models.database import connect, init_db, reset_connections


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "care.db"))
    reset_connections()
    init_db()
    group_care.clear("fixture-group", "000001")
    yield
    group_care.clear("fixture-group", "000001")
    reset_connections()


def _relationship(familiarity=0.4, affinity=0.0):
    return {"familiarity": familiarity, "affinity": affinity}


def _scene():
    return {"atmosphere": "emotional", "has_recent_bot_reply": False}


def test_care_requires_familiar_member_and_rejects_sensitive_or_crisis(db_env):
    assert group_care.assess(
        "fixture-group", "000001", "最近压力好大", _relationship(0.1), _scene()
    ).reason == "member_not_familiar"
    assert group_care.assess(
        "fixture-group", "000001", "最近压力好大，想自杀", _relationship(), _scene()
    ).reason == "crisis_signal"
    assert group_care.assess(
        "fixture-group", "000001", "我忘了账号密码很焦虑", _relationship(), _scene()
    ).reason == "sensitive_subject"


def test_care_has_cooldown_one_followup_and_daily_limit(db_env):
    start = datetime(2026, 9, 13, 9, tzinfo=timezone.utc)
    first = group_care.assess(
        "fixture-group", "000001", "最近压力好大，感觉很累", _relationship(), _scene(), now=start
    )
    assert first.eligible is True
    assert first.kind == "initial"
    group_care.record_sent("fixture-group", "000001", first.kind, now=start)

    assert group_care.assess(
        "fixture-group", "000001", "还是有点累", _relationship(), _scene(), now=start + timedelta(hours=1)
    ).reason == "cooldown"
    followup = group_care.assess(
        "fixture-group", "000001", "今天还是有点难受", _relationship(), _scene(), now=start + timedelta(hours=7)
    )
    assert followup.eligible is True
    assert followup.kind == "followup"
    group_care.record_sent("fixture-group", "000001", followup.kind, now=start + timedelta(hours=7))

    assert group_care.assess(
        "fixture-group", "000001", "又开始烦了", _relationship(), _scene(), now=start + timedelta(hours=8)
    ).reason == "daily_limit"
    assert group_care.assess(
        "fixture-group", "000001", "第二天还是很累", _relationship(), _scene(),
        now=start + timedelta(hours=23),
    ).reason == "followup_limit"

    conn = connect()
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(group_care_events)")}
        assert "content" not in columns
        assert conn.execute("SELECT COUNT(*) FROM group_care_events").fetchone()[0] == 2
    finally:
        conn.close()


def test_care_isolated_by_group_and_user(db_env):
    start = datetime(2026, 9, 13, 9, tzinfo=timezone.utc)
    decision = group_care.assess(
        "fixture-group", "000001", "最近有点沮丧", _relationship(), _scene(), now=start
    )
    assert decision.eligible is True
    group_care.record_sent("fixture-group", "000001", decision.kind, now=start)

    other_group = group_care.assess(
        "other-group", "000001", "最近有点沮丧", _relationship(), _scene(), now=start + timedelta(minutes=1)
    )
    other_user = group_care.assess(
        "fixture-group", "000002", "最近有点沮丧", _relationship(), _scene(), now=start + timedelta(minutes=1)
    )
    assert other_group.eligible is True
    assert other_user.eligible is True


def test_care_skips_tense_scene_and_recent_bot_reply(db_env):
    assert group_care.assess(
        "fixture-group", "000001", "最近有点焦虑", _relationship(), {"atmosphere": "tense"}
    ).reason == "tense_scene"
    assert group_care.assess(
        "fixture-group", "000001", "最近有点焦虑", _relationship(), {"has_recent_bot_reply": True}
    ).reason == "recent_bot_reply"
