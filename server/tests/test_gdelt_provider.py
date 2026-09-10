"""GDELT 检索源测试：字段映射、限流降级、代理隔离、绝不抛出。"""
import asyncio

import pytest

from app.chat import gdelt_provider as gp
from app.config import settings


class _Resp:
    def __init__(self, *, payload=None, text="", status=200):
        self._payload = payload
        self._text = text if text else ('{"articles":[]}' if payload is None else "{}")
        self.status_code = status

    def json(self):
        if self._payload is None:
            raise ValueError("bad json")
        return self._payload

    @property
    def text(self):
        return self._text


class _Client:
    behavior = None
    last_kwargs = None

    def __init__(self, *args, **kwargs):
        type(self).last_kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        return type(self).behavior(url, kwargs)


@pytest.fixture(autouse=True)
def enable_gdelt(monkeypatch):
    monkeypatch.setattr(settings, "gdelt_enabled", True)
    monkeypatch.setattr(settings, "gdelt_proxy", "http://127.0.0.1:7890")
    monkeypatch.setattr(settings, "gdelt_timeout", 10.0)
    monkeypatch.setattr(settings, "gdelt_max_results", 10)
    # 关掉节流，避免测试间等待
    monkeypatch.setattr(gp, "_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(gp, "_last_call", 0.0)
    yield


@pytest.fixture
def fake_http(monkeypatch):
    def install(handler):
        _Client.behavior = staticmethod(handler)
        monkeypatch.setattr(gp.httpx, "AsyncClient", _Client)
        return handler
    return install


_SAMPLE = {
    "articles": [
        {
            "url": "https://www.example.com/gb/a1.htm",
            "title": "某地事件报道",
            "seendate": "20260907T224500Z",
            "domain": "example.com",
            "language": "Chinese",
            "sourcecountry": "China",
        }
    ]
}


def test_maps_gdelt_fields_to_standard_schema(fake_http):
    fake_http(lambda url, kw: _Resp(payload=_SAMPLE))
    out = asyncio.run(gp.search("湖南四岁幼童事件"))
    assert len(out) == 1
    item = out[0]
    assert item["title"] == "某地事件报道"
    assert item["source"] == "example.com"
    assert item["published_at"] == "2026-09-07T22:45:00Z"   # seendate 转 ISO
    assert item["time_known"] is True
    assert item["language"] == "Chinese"


def test_seendate_conversion():
    assert gp._seendate_to_iso("20260907T224500Z") == "2026-09-07T22:45:00Z"
    assert gp._seendate_to_iso("") == ""
    assert gp._seendate_to_iso("garbage") == ""


def test_rate_limit_429_degrades_to_empty(fake_http):
    """限流返回纯文本 → 空列表，不抛出。"""
    fake_http(lambda url, kw: _Resp(text="Please limit requests", status=429))
    assert asyncio.run(gp.search("x")) == []


def test_non_json_text_degrades_to_empty(fake_http):
    fake_http(lambda url, kw: _Resp(text="not json", status=200))
    assert asyncio.run(gp.search("x")) == []


def test_http_error_degrades_to_empty(fake_http):
    def boom(url, kw):
        raise gp.httpx.ConnectError("proxy down")
    fake_http(boom)
    assert asyncio.run(gp.search("x")) == []


def test_disabled_returns_empty(monkeypatch, fake_http):
    monkeypatch.setattr(settings, "gdelt_enabled", False)
    called = {"n": 0}

    def handler(url, kw):
        called["n"] += 1
        return _Resp(payload=_SAMPLE)
    fake_http(handler)
    assert asyncio.run(gp.search("x")) == []
    assert called["n"] == 0, "禁用时不应发起请求"


def test_empty_query_returns_empty(fake_http):
    fake_http(lambda url, kw: _Resp(payload=_SAMPLE))
    assert asyncio.run(gp.search("   ")) == []


def test_proxy_passed_to_client(fake_http):
    fake_http(lambda url, kw: _Resp(payload=_SAMPLE))
    asyncio.run(gp.search("x"))
    assert _Client.last_kwargs.get("proxy") == "http://127.0.0.1:7890"
    assert _Client.last_kwargs.get("trust_env") is False


def test_empty_proxy_means_direct(monkeypatch, fake_http):
    monkeypatch.setattr(settings, "gdelt_proxy", "")
    fake_http(lambda url, kw: _Resp(payload=_SAMPLE))
    asyncio.run(gp.search("x"))
    assert _Client.last_kwargs.get("proxy") is None


def test_dedup_and_cap(fake_http, monkeypatch):
    monkeypatch.setattr(settings, "gdelt_max_results", 2)
    payload = {"articles": [
        {"url": "https://a.com/1", "title": "t1", "seendate": "20260907T000000Z", "domain": "a.com"},
        {"url": "https://a.com/1", "title": "dup", "seendate": "20260907T000000Z", "domain": "a.com"},
        {"url": "https://b.com/2", "title": "t2", "seendate": "20260907T000000Z", "domain": "b.com"},
        {"url": "https://c.com/3", "title": "t3", "seendate": "20260907T000000Z", "domain": "c.com"},
    ]}
    fake_http(lambda url, kw: _Resp(payload=payload))
    out = asyncio.run(gp.search("x"))
    assert len(out) == 2                       # cap=2
    assert {i["url"] for i in out} == {"https://a.com/1", "https://b.com/2"}
