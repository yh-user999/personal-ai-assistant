"""群画像提取测试：短消息跳过、敏感维度过滤、LLM 失败不抛、注入按群隔离。"""
import asyncio

import pytest

from app.config import settings
from app.models.database import init_db, reset_connections
from app.services import group_profile, group_profile_extract


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    reset_connections()
    init_db()
    group_profile.ensure_table()
    yield
    reset_connections()


def _fake_llm(monkeypatch, payload):
    async def fake_chat_json(system, prompt, **kwargs):
        if isinstance(payload, Exception):
            raise payload
        return payload

    from app.core import llm

    monkeypatch.setattr(llm, "chat_json", fake_chat_json)


def test_short_message_is_skipped(db_env, monkeypatch):
    called = {"n": 0}

    async def should_not_run(*a, **k):
        called["n"] += 1
        return {}

    from app.core import llm

    monkeypatch.setattr(llm, "chat_json", should_not_run)
    n = asyncio.run(group_profile_extract.maybe_extract("456", "10086", "嗯"))
    assert n == 0 and called["n"] == 0, "过短发言不应触发 LLM"


def test_extracts_allowed_dimensions(db_env, monkeypatch):
    _fake_llm(monkeypatch, {"updates": [
        {"dimension": "topics", "value": "喜欢聊天文和摄影", "confidence": 0.7},
        {"dimension": "style", "value": "说话简短", "confidence": 0.6},
    ]})
    n = asyncio.run(group_profile_extract.maybe_extract("456", "10086", "我平时爱拍星空，也看天文科普"))
    assert n == 2
    text = group_profile.get_injection("456", "10086")
    assert "天文" in text and "简短" in text


def test_sensitive_dimensions_never_persisted(db_env, monkeypatch):
    """即使模型返回敏感维度，也必须被拦在入库之前。"""
    _fake_llm(monkeypatch, {"updates": [
        {"dimension": "health", "value": "有慢性病", "confidence": 0.9},
        {"dimension": "politics", "value": "某立场", "confidence": 0.9},
        {"dimension": "income", "value": "月入很高", "confidence": 0.9},
    ]})
    n = asyncio.run(group_profile_extract.maybe_extract("456", "10086", "今天去医院复查了一下老毛病"))
    assert n == 0
    text = group_profile.get_injection("456", "10086")
    assert text == "" and "慢性病" not in text


def test_llm_failure_is_silent(db_env, monkeypatch):
    """提取失败不能抛出：它是后台任务，不应影响群回复。"""
    _fake_llm(monkeypatch, RuntimeError("backend down"))
    assert asyncio.run(group_profile_extract.maybe_extract("456", "10086", "一句正常长度的群聊发言")) == 0


def test_malformed_llm_output_is_tolerated(db_env, monkeypatch):
    _fake_llm(monkeypatch, {"unexpected": "shape"})
    assert asyncio.run(group_profile_extract.maybe_extract("456", "10086", "一句正常长度的群聊发言")) == 0


def test_extraction_is_scoped_per_group(db_env, monkeypatch):
    _fake_llm(monkeypatch, {"updates": [
        {"dimension": "topics", "value": "只属于A群的话题", "confidence": 0.8},
    ]})
    asyncio.run(group_profile_extract.maybe_extract("456", "10086", "这是一句足够长的群聊发言内容"))
    assert "A群" in group_profile.get_injection("456", "10086")
    assert group_profile.get_injection("789", "10086") == "", "画像不得跨群可见"
