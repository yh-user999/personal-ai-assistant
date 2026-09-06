"""llm.chat_stream 流式故障切换回归。

锁定语义：首个内容增量输出前失败可切换 Key 重试；已输出增量后失败直接抛出
（换 Key 重发会导致客户端收到重复内容）；usage 缺失时跳过记账不报错。
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.config import settings
from app.core import llm


class _StatusError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"status={status_code}")
        self.status_code = status_code


class _Usage:
    def __init__(self, prompt=10, completion=3):
        self.prompt_tokens = prompt
        self.completion_tokens = completion


class _Chunk:
    """OpenAI 流式 chunk 桩：text=None 且 usage 非空时为末尾的纯 usage chunk。"""

    def __init__(self, text=None, usage=None):
        if text is None and usage is not None:
            self.choices = []
        else:
            self.choices = [SimpleNamespace(delta=SimpleNamespace(content=text))]
        self.usage = usage


def _stream(*chunks_or_errors):
    async def _gen():
        for item in chunks_or_errors:
            if isinstance(item, BaseException):
                raise item
            yield item

    return _gen


class _FakeCompletions:
    def __init__(self, key_index, scripts, calls):
        self.key_index = key_index
        self.scripts = scripts
        self.calls = calls

    async def create(self, **kwargs):
        self.calls.append((self.key_index, kwargs))
        actions = self.scripts[self.key_index]
        action = actions.pop(0) if actions else None
        if isinstance(action, BaseException):
            raise action
        if kwargs.get("stream"):
            assert kwargs.get("stream_options") == {"include_usage": True}
            maker = action if action is not None else _stream(_Chunk("ok"))
            return maker()
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))], usage=None
        )


def _factory(scripts, calls):
    def make_client(**kwargs):
        key_index = int(kwargs["api_key"].split("-")[-1])
        return SimpleNamespace(
            chat=SimpleNamespace(completions=_FakeCompletions(key_index, scripts, calls))
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
    monkeypatch.setattr(
        settings, "llm_api_keys", ",".join(f"test-key-{i}" for i in range(count))
    )


async def _collect(gen):
    return [text async for text in gen]


def test_stream_yields_deltas_and_records_usage(monkeypatch):
    _set_keys(monkeypatch, 1)
    calls = []
    scripts = {
        0: [_stream(_Chunk("你"), _Chunk("好"), _Chunk(None, usage=_Usage()))],
    }
    monkeypatch.setattr(llm, "AsyncOpenAI", _factory(scripts, calls))

    deltas = asyncio.run(
        _collect(llm.chat_stream([{"role": "user", "content": "hi"}]))
    )

    assert deltas == ["你", "好"]
    assert calls[0][1]["stream"] is True
    details = llm.get_usage_details()
    assert details["usage"]["calls"] == 1
    assert details["last_call"]["fallback_count"] == 0


def test_stream_switches_key_before_first_delta(monkeypatch):
    _set_keys(monkeypatch, 2)
    calls = []
    scripts = {
        0: [_StatusError(429)],
        1: [_stream(_Chunk("recovered"))],
    }
    monkeypatch.setattr(llm, "AsyncOpenAI", _factory(scripts, calls))

    deltas = asyncio.run(_collect(llm.chat_stream([])))

    assert deltas == ["recovered"]
    assert [index for index, _ in calls] == [0, 1]
    assert llm.get_usage_details()["fallback_count"] == 1


def test_stream_does_not_switch_after_emission(monkeypatch):
    """已输出增量后连接中断：直接抛出，绝不换 Key 重发造成重复输出。"""
    _set_keys(monkeypatch, 2)
    calls = []
    scripts = {
        0: [_stream(_Chunk("部分"), ConnectionError("dropped"))],
        1: [_stream(_Chunk("must-not-run"))],
    }
    monkeypatch.setattr(llm, "AsyncOpenAI", _factory(scripts, calls))

    with pytest.raises(ConnectionError):
        asyncio.run(_collect(llm.chat_stream([])))

    assert [index for index, _ in calls] == [0]
    assert llm.get_usage_details()["requests"] == 0


def test_stream_non_retryable_error_does_not_switch(monkeypatch):
    _set_keys(monkeypatch, 2)
    calls = []
    original = _StatusError(400)
    scripts = {0: [original], 1: [_stream(_Chunk("x"))]}
    monkeypatch.setattr(llm, "AsyncOpenAI", _factory(scripts, calls))

    with pytest.raises(_StatusError) as caught:
        asyncio.run(_collect(llm.chat_stream([])))

    assert caught.value is original
    assert [index for index, _ in calls] == [0]


def test_stream_without_usage_chunk_skips_accounting(monkeypatch):
    """中转服务可能不下发 usage：缺失时静默跳过记账。"""
    _set_keys(monkeypatch, 1)
    calls = []
    scripts = {0: [_stream(_Chunk("a"), _Chunk("b"))]}
    monkeypatch.setattr(llm, "AsyncOpenAI", _factory(scripts, calls))

    deltas = asyncio.run(_collect(llm.chat_stream([])))

    assert deltas == ["a", "b"]
    assert llm.get_usage_details()["usage"]["calls"] == 0
