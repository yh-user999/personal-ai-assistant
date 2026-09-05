"""LLM 多 Key 配置、故障切换、观测与小说模型选择回归。"""
import asyncio
from types import SimpleNamespace

import pytest

from app.config import (
    Settings,
    _validate_llm_config,
    mask_api_key,
    parse_llm_api_keys,
    settings,
)
from app.core import llm


class _StatusError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"status={status_code}")
        self.status_code = status_code


class _Usage:
    def __init__(self, prompt=10, completion=3):
        self.prompt_tokens = prompt
        self.completion_tokens = completion


class _Response:
    def __init__(self, content="ok", usage=None):
        self.usage = usage
        self.choices = [
            SimpleNamespace(message=SimpleNamespace(content=content))
        ]


class _FakeCompletions:
    def __init__(self, key_index, scripts, calls):
        self.key_index = key_index
        self.scripts = scripts
        self.calls = calls

    async def create(self, **kwargs):
        self.calls.append((self.key_index, kwargs))
        actions = self.scripts[self.key_index]
        action = actions.pop(0) if actions else _Response()
        if isinstance(action, BaseException):
            raise action
        return action


class _FakeModels:
    def __init__(self, model_ids):
        self.model_ids = model_ids

    async def list(self, **kwargs):
        return SimpleNamespace(
            data=[SimpleNamespace(id=model_id) for model_id in self.model_ids]
        )


def _factory(scripts, calls, model_ids=()):
    def make_client(**kwargs):
        key_index = int(kwargs["api_key"].split("-")[-1])
        return SimpleNamespace(
            chat=SimpleNamespace(
                completions=_FakeCompletions(key_index, scripts, calls)
            ),
            models=_FakeModels(model_ids),
        )

    return make_client


@pytest.fixture(autouse=True)
def clean_llm_state(monkeypatch):
    monkeypatch.setattr(settings, "llm_key_cooldown_seconds", 0.0)
    llm.reset_client_pool()
    llm.reset_usage()
    yield
    llm.reset_client_pool()
    llm.reset_usage()


def _set_keys(monkeypatch, count: int):
    values = [f"test-key-{index}" for index in range(count)]
    monkeypatch.setattr(settings, "llm_api_keys", ",".join(values))
    return values


def test_parse_multi_keys_and_legacy_compatibility():
    assert parse_llm_api_keys("first-key,\n second-key") == ["first-key", "second-key"]
    assert parse_llm_api_keys("", "legacy-key") == ["legacy-key"]


def test_parse_rejects_empty_duplicate_and_too_many_keys():
    with pytest.raises(ValueError, match="为空"):
        parse_llm_api_keys("first-key,,second-key")
    with pytest.raises(ValueError, match="重复"):
        parse_llm_api_keys("first-key,first-key")
    with pytest.raises(ValueError, match="最多支持"):
        parse_llm_api_keys(",".join(f"key-{i}" for i in range(9)))


def test_production_key_length_validation_and_masking():
    production = Settings(
        _env_file=None,
        deployment_env="production",
        llm_api_keys="short",
    )
    with pytest.raises(ValueError, match="长度不足"):
        _validate_llm_config(production)

    masked = mask_api_key("secret-value", index=1)
    assert "secret-value" not in masked
    assert masked.startswith("key[1]#")


def test_rate_limit_switches_to_next_key_and_records_observability(monkeypatch):
    values = _set_keys(monkeypatch, 2)
    calls = []
    scripts = {
        0: [_StatusError(429)],
        1: [_Response("from-second", _Usage(prompt=20, completion=5))],
    }
    monkeypatch.setattr(llm, "AsyncOpenAI", _factory(scripts, calls))

    result = asyncio.run(
        llm.chat(
            [{"role": "user", "content": "hello"}],
            model="novel-model",
        )
    )

    assert result == "from-second"
    assert [index for index, _ in calls] == [0, 1]
    assert all(values[index] not in str(kwargs) for index, kwargs in calls)
    details = llm.get_usage_details()
    assert details["fallback_count"] == 1
    assert details["last_call"] == {
        "key_index": 1,
        "model": "novel-model",
        "fallback_count": 1,
    }
    assert details["failures"] == {"0": {"http_429": 1}}
    assert details["usage"]["calls"] == 1


def test_timeout_switches_to_next_key(monkeypatch):
    _set_keys(monkeypatch, 2)
    calls = []
    scripts = {
        0: [TimeoutError("network timeout")],
        1: [_Response("recovered")],
    }
    monkeypatch.setattr(llm, "AsyncOpenAI", _factory(scripts, calls))

    assert asyncio.run(llm.chat([])) == "recovered"
    assert [index for index, _ in calls] == [0, 1]
    assert llm.get_usage_details()["failures"] == {"0": {"timeout": 1}}


def test_non_retryable_4xx_does_not_switch(monkeypatch):
    _set_keys(monkeypatch, 2)
    calls = []
    original = _StatusError(400)
    scripts = {0: [original], 1: [_Response("must-not-run")]}
    monkeypatch.setattr(llm, "AsyncOpenAI", _factory(scripts, calls))

    with pytest.raises(_StatusError) as caught:
        asyncio.run(llm.chat([]))

    assert caught.value is original
    assert [index for index, _ in calls] == [0]
    assert llm.get_usage_details()["failures"] == {"0": {"http_400": 1}}


def test_all_keys_fail_only_attempts_one_round(monkeypatch):
    _set_keys(monkeypatch, 3)
    calls = []
    scripts = {
        0: [_StatusError(500)],
        1: [_StatusError(502)],
        2: [_StatusError(503)],
    }
    monkeypatch.setattr(llm, "AsyncOpenAI", _factory(scripts, calls))

    with pytest.raises(_StatusError) as caught:
        asyncio.run(llm.chat([]))

    assert caught.value.status_code == 503
    assert [index for index, _ in calls] == [0, 1, 2]
    assert llm.get_usage_details()["requests"] == 0
    assert sum(item["failures"] for item in llm.get_usage_details()["by_key"].values()) == 3


def test_regular_chat_uses_global_model_and_novel_model_check_falls_back(monkeypatch):
    _set_keys(monkeypatch, 1)
    monkeypatch.setattr(settings, "llm_model", "global-model")
    monkeypatch.setattr(settings, "novel_llm_model", "missing-novel-model")
    calls = []
    scripts = {0: [_Response("global") ]}
    monkeypatch.setattr(llm, "AsyncOpenAI", _factory(scripts, calls, model_ids=["global-model"]))

    assert asyncio.run(llm.chat([])) == "global"
    assert calls[0][1]["model"] == "global-model"
    assert asyncio.run(llm.validate_novel_model()) == "global-model"
    assert llm.get_novel_model() == "global-model"


def test_novel_model_check_keeps_available_model(monkeypatch):
    _set_keys(monkeypatch, 1)
    monkeypatch.setattr(settings, "llm_model", "global-model")
    monkeypatch.setattr(settings, "novel_llm_model", "novel-model")
    calls = []
    monkeypatch.setattr(
        llm,
        "AsyncOpenAI",
        _factory({0: []}, calls, model_ids=["global-model", "novel-model"]),
    )

    assert asyncio.run(llm.validate_novel_model()) == "novel-model"
    assert llm.get_novel_model() == "novel-model"
