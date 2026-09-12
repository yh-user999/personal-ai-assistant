"""群画像提取测试：写入按 QQ 号归属的统一画像，敏感维度拒绝，失败静默。"""
import asyncio

import pytest

from app.config import settings
from app.models.database import connect, init_db, reset_connections
from app.services import group_profile_extract
from app.services import profile as profile_service


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    reset_connections()
    init_db()
    yield
    reset_connections()


def _fake_llm(monkeypatch, payload):
    async def fake_chat_json(system, prompt, **kwargs):
        if isinstance(payload, Exception):
            raise payload
        return payload

    from app.core import llm

    monkeypatch.setattr(llm, "chat_json", fake_chat_json)


# ── 基本提取 ───────────────────────────────────────────────

def test_short_message_is_skipped(db_env, monkeypatch):
    called = {"n": 0}

    async def should_not_run(*a, **k):
        called["n"] += 1
        return {}

    from app.core import llm

    monkeypatch.setattr(llm, "chat_json", should_not_run)
    assert asyncio.run(group_profile_extract.maybe_extract("456", "10086", "嗯")) == 0
    assert called["n"] == 0, "过短发言不应触发 LLM"


def test_extracts_allowed_dimensions(db_env, monkeypatch):
    _fake_llm(monkeypatch, {"updates": [
        {"dimension": "topics", "value": "喜欢聊天文和摄影", "confidence": 0.7},
        {"dimension": "style", "value": "说话简短", "confidence": 0.6},
    ]})
    assert asyncio.run(
        group_profile_extract.maybe_extract("456", "10086", "我平时爱拍星空，也看天文科普")
    ) == 2
    text = profile_service.get_profile_injection(user_id="10086")
    assert "天文" in text and "简短" in text


# ── 归属：按 QQ 号，而非按场景 ──────────────────────────────

def test_profile_belongs_to_qq_number_not_scene(db_env, monkeypatch):
    """同一 QQ 号在群里形成的画像，私聊时同样可用——画像以人为单位。"""
    _fake_llm(monkeypatch, {"updates": [
        {"dimension": "topics", "value": "关注航天发射", "confidence": 0.8},
    ]})
    asyncio.run(group_profile_extract.maybe_extract("456", "10086", "最近的火箭发射我基本每次都在追"))

    # 群里学到的偏好，在私聊（同一 uid）读取时也应存在
    assert "航天" in profile_service.get_profile_injection(user_id="10086")


def test_other_users_profiles_are_not_visible(db_env, monkeypatch):
    """按 user_id 查询天然隔离：读某人画像不会带出别人的。"""
    _fake_llm(monkeypatch, {"updates": [
        {"dimension": "topics", "value": "只属于甲的偏好", "confidence": 0.8},
    ]})
    asyncio.run(group_profile_extract.maybe_extract("456", "10086", "这是一句足够长的群聊发言"))

    assert "甲" in profile_service.get_profile_injection(user_id="10086")
    assert profile_service.get_profile_injection(user_id="20000") == ""


def test_owner_profile_is_not_polluted_by_group_members(db_env, monkeypatch):
    """群成员画像不得写到主人名下。"""
    _fake_llm(monkeypatch, {"updates": [
        {"dimension": "topics", "value": "群友的话题", "confidence": 0.8},
    ]})
    asyncio.run(group_profile_extract.maybe_extract("456", "10086", "这是一句足够长的群聊发言"))

    conn = connect()
    try:
        owners = conn.execute(
            "SELECT COUNT(*) FROM profile WHERE user_id='owner'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert owners == 0, "群成员画像不得记到主人名下"


# ── 敏感维度与健壮性 ───────────────────────────────────────

def test_sensitive_dimensions_never_persisted(db_env, monkeypatch):
    _fake_llm(monkeypatch, {"updates": [
        {"dimension": "health", "value": "有慢性病", "confidence": 0.9},
        {"dimension": "politics", "value": "某立场", "confidence": 0.9},
        {"dimension": "income", "value": "月入很高", "confidence": 0.9},
    ]})
    assert asyncio.run(
        group_profile_extract.maybe_extract("456", "10086", "今天去医院复查了一下老毛病")
    ) == 0
    assert profile_service.get_profile_injection(user_id="10086") == ""


def test_parse_updates_filters_sensitive_and_invalid():
    out = profile_service.parse_updates({"updates": [
        {"dimension": "topics", "value": "聊摄影", "confidence": 0.8},
        {"dimension": "health", "value": "有慢性病", "confidence": 0.9},
        {"dimension": "style", "value": "", "confidence": 0.7},
        {"dimension": "preferred_name", "value": "老王", "confidence": "bad"},
    ]})
    assert [d for d, _v, _c in out] == ["topics", "preferred_name"]
    assert out[1][2] == 0.5, "非法 confidence 应回落默认值"


def test_parse_updates_tolerates_garbage():
    assert profile_service.parse_updates("not json") == []
    assert profile_service.parse_updates(None) == []
    assert profile_service.parse_updates({"updates": ["bad", 42]}) == []


def test_llm_failure_is_silent(db_env, monkeypatch):
    _fake_llm(monkeypatch, RuntimeError("backend down"))
    assert asyncio.run(
        group_profile_extract.maybe_extract("456", "10086", "一句正常长度的群聊发言")
    ) == 0


def test_malformed_llm_output_is_tolerated(db_env, monkeypatch):
    _fake_llm(monkeypatch, {"unexpected": "shape"})
    assert asyncio.run(
        group_profile_extract.maybe_extract("456", "10086", "一句正常长度的群聊发言")
    ) == 0


def test_forget_user_removes_profile(db_env, monkeypatch):
    """支持"别记我"：删除某人全部画像。"""
    _fake_llm(monkeypatch, {"updates": [
        {"dimension": "topics", "value": "话题", "confidence": 0.8},
        {"dimension": "style", "value": "风格", "confidence": 0.8},
    ]})
    asyncio.run(group_profile_extract.maybe_extract("456", "10086", "这是一句足够长的群聊发言"))
    assert profile_service.forget_user("10086") == 2
    assert profile_service.get_profile_injection(user_id="10086") == ""


def test_long_value_is_truncated(db_env):
    profile_service.remember("10086", "topics", "话" * 1000)
    conn = connect()
    try:
        value = conn.execute(
            "SELECT value FROM profile WHERE user_id='10086'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert len(value) <= profile_service.MAX_VALUE_CHARS
