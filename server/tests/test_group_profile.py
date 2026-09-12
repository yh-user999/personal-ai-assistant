"""群成员画像测试：维度收敛、敏感属性拒绝、与私聊画像物理隔离、可遗忘。"""
import pytest

from app.config import settings
from app.models.database import connect, init_db, reset_connections
from app.services import group_profile


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    reset_connections()
    init_db()
    group_profile.ensure_table()
    yield
    reset_connections()


# ── 基本可用性 ─────────────────────────────────────────────

def test_remember_and_inject_roundtrip(db_env):
    assert group_profile.remember("456", "10086", "topics", "喜欢聊科幻和航天")
    text = group_profile.get_injection("456", "10086")
    assert "科幻" in text
    # 注入文本须明确要求模型别暴露画像存在，避免群里让人察觉被记录。
    assert "不要主动复述" in text and "不要说明你有记录" in text


def test_missing_profile_returns_empty(db_env):
    assert group_profile.get_injection("456", "99999") == ""


# ── 维度收敛：只记对话所需 ──────────────────────────────────

def test_sensitive_dimensions_are_rejected(db_env):
    for dim in ("politics", "religion", "health", "income", "address", "phone"):
        assert group_profile.remember("456", "10086", dim, "任意值") is False
        assert dim not in group_profile.get_injection("456", "10086")


def test_unknown_dimension_is_rejected(db_env):
    assert group_profile.remember("456", "10086", "credit_score", "700") is False
    assert group_profile.get_injection("456", "10086") == ""


def test_parse_updates_filters_sensitive_and_invalid():
    payload = {
        "updates": [
            {"dimension": "topics", "value": "聊摄影", "confidence": 0.8},
            {"dimension": "health", "value": "有慢性病", "confidence": 0.9},
            {"dimension": "politics", "value": "某立场", "confidence": 0.9},
            {"dimension": "style", "value": "", "confidence": 0.7},
            {"dimension": "preferred_name", "value": "老王", "confidence": "bad"},
        ]
    }
    out = group_profile.parse_updates(payload)
    dims = [d for d, _v, _c in out]
    assert dims == ["topics", "preferred_name"]
    assert ("health" in dims) is False and ("politics" in dims) is False
    # 非法 confidence 回落默认值而不是崩溃
    assert out[1][2] == 0.5


def test_parse_updates_tolerates_garbage():
    assert group_profile.parse_updates("not json") == []
    assert group_profile.parse_updates(None) == []
    assert group_profile.parse_updates({"updates": ["bad", 42]}) == []


# ── 隔离（核心保证）────────────────────────────────────────

def test_group_profile_never_touches_private_profile_table(db_env):
    """群画像必须落在独立表，不得污染私聊 profile。"""
    group_profile.remember("456", "10086", "topics", "群里聊的话题")

    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM profile").fetchone()[0] == 0, \
            "群画像不得写入私聊 profile 表"
        assert conn.execute(
            "SELECT COUNT(*) FROM group_member_profile"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_private_profile_is_not_readable_from_group_scope(db_env):
    """群作用域读画像时，绝不能读到主人的私聊画像。"""
    from datetime import datetime, timezone

    conn = connect()
    try:
        conn.execute(
            "INSERT INTO profile (user_id, dimension, value, confidence, updated_at) VALUES (?,?,?,?,?)",
            ("owner", "technical_background", "主人的私密技术背景", 0.9,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    assert group_profile.get_injection("456", "owner") == ""
    assert "私密" not in group_profile.get_injection("456", "10086")


def test_profiles_are_isolated_between_groups(db_env):
    group_profile.remember("456", "10086", "topics", "A群聊摄影")
    group_profile.remember("789", "10086", "topics", "B群聊编程")

    a = group_profile.get_injection("456", "10086")
    b = group_profile.get_injection("789", "10086")
    assert "摄影" in a and "编程" not in a
    assert "编程" in b and "摄影" not in b


# ── 可遗忘与有界 ───────────────────────────────────────────

def test_forget_member_removes_all_dimensions(db_env):
    group_profile.remember("456", "10086", "topics", "话题")
    group_profile.remember("456", "10086", "style", "简短")
    assert group_profile.forget_member("456", "10086") == 2
    assert group_profile.get_injection("456", "10086") == ""


def test_forget_group_removes_only_that_group(db_env):
    group_profile.remember("456", "1", "topics", "A")
    group_profile.remember("789", "2", "topics", "B")
    assert group_profile.forget_group("456") == 1
    assert group_profile.get_injection("456", "1") == ""
    assert group_profile.get_injection("789", "2") != ""


def test_expired_records_are_purged(db_env):
    from datetime import datetime, timedelta, timezone

    group_profile.remember("456", "10086", "topics", "很久以前")
    conn = connect()
    try:
        old = (datetime.now(timezone.utc) - timedelta(days=group_profile.RETENTION_DAYS + 5)).isoformat()
        conn.execute("UPDATE group_member_profile SET updated_at=?", (old,))
        conn.commit()
    finally:
        conn.close()

    assert group_profile.purge_expired() == 1
    assert group_profile.get_injection("456", "10086") == ""


def test_long_value_is_truncated(db_env):
    group_profile.remember("456", "10086", "topics", "话" * 1000)
    conn = connect()
    try:
        value = conn.execute("SELECT value FROM group_member_profile").fetchone()[0]
    finally:
        conn.close()
    assert len(value) <= group_profile.MAX_VALUE_CHARS
