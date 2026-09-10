"""国内热榜检索源测试：多源解析、单源失败隔离、去重、绝不抛出。"""
import asyncio

import pytest

from app.chat import hotboard_provider as hb
from app.config import settings


class _Resp:
    def __init__(self, *, payload=None, text=None, status=200):
        self._payload = payload
        self._text = text if text is not None else ("{}" if payload is None else "{}")
        self.status_code = status

    def json(self):
        if self._payload is None:
            raise ValueError("bad")
        return self._payload

    @property
    def text(self):
        return self._text if self._text else ("{}" if self._payload is not None else "x")


class _Client:
    routes: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False

    async def get(self, url, **kw):
        for frag, resp in type(self).routes.items():
            if frag in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return _Resp(text="x", status=404)


@pytest.fixture(autouse=True)
def enable(monkeypatch):
    monkeypatch.setattr(settings, "hotboard_enabled", True)
    monkeypatch.setattr(settings, "hotboard_timeout", 5.0)
    monkeypatch.setattr(settings, "hotboard_max_items", 15)
    yield


@pytest.fixture
def routes(monkeypatch):
    def install(mapping):
        _Client.routes = mapping
        monkeypatch.setattr(hb.httpx, "AsyncClient", _Client)
    return install


_TT = {"data": [
    {"Title": "习近平对某事作出重要指示", "Url": "https://a.com/1", "HotValue": "9999"},
    {"Title": "某地发生重大新闻", "Url": "https://a.com/2", "HotValue": "8888"},
]}
_BD = {"data": {"cards": [{"content": [
    {"word": "某热搜话题", "url": "https://b.com/1", "hotScore": "7777"},
    {"word": "某地发生重大新闻", "url": "https://b.com/2"},   # 与头条重复
]}]}}


def test_merges_both_sources(routes):
    routes({"toutiao.com": _Resp(payload=_TT), "baidu.com": _Resp(payload=_BD)})
    out = asyncio.run(hb.fetch_hotboard())
    titles = [i["title"] for i in out]
    assert "习近平对某事作出重要指示" in titles
    assert "某热搜话题" in titles
    # 重复标题只留一条
    assert titles.count("某地发生重大新闻") == 1
    sources = {i["source"] for i in out}
    assert sources == {"今日头条热榜", "百度热搜"}


def test_one_source_failure_keeps_other(routes):
    routes({"toutiao.com": _Resp(payload=_TT), "baidu.com": ConnectionError("down")})
    out = asyncio.run(hb.fetch_hotboard())
    assert out and all(i["source"] == "今日头条热榜" for i in out)


def test_all_fail_returns_empty(routes):
    routes({"toutiao.com": _Resp(text="x", status=500),
            "baidu.com": _Resp(text="x", status=500)})
    assert asyncio.run(hb.fetch_hotboard()) == []


def test_non_json_returns_empty(routes):
    routes({"toutiao.com": _Resp(text="<html>", status=200),
            "baidu.com": _Resp(text="<html>", status=200)})
    assert asyncio.run(hb.fetch_hotboard()) == []


def test_disabled_no_request(monkeypatch, routes):
    monkeypatch.setattr(settings, "hotboard_enabled", False)
    hit = {"n": 0}

    class _Spy(_Client):
        async def get(self, url, **kw):
            hit["n"] += 1
            return _Resp(payload=_TT)
    monkeypatch.setattr(hb.httpx, "AsyncClient", _Spy)
    assert asyncio.run(hb.fetch_hotboard()) == []
    assert hit["n"] == 0


def test_cap_limits_items(routes, monkeypatch):
    monkeypatch.setattr(settings, "hotboard_max_items", 2)
    big = {"data": [{"Title": f"标题{i}", "Url": f"https://a.com/{i}", "HotValue": "1"} for i in range(10)]}
    routes({"toutiao.com": _Resp(payload=big), "baidu.com": _Resp(payload={"data": {"cards": []}})})
    out = asyncio.run(hb.fetch_hotboard())
    assert len(out) == 2


def test_format_hotboard():
    items = [{"title": "热点一", "url": "", "source": "今日头条热榜", "hot": "9"}]
    text = hb.format_hotboard(items)
    assert "热点一" in text
    assert "非已核实事实" in text
    assert hb.format_hotboard([]) == ""


# ── 路由：浏览型 vs 具体事件 ──────────────────────────────────

def test_hot_browsing_routing():
    from app.chat import web_provider as wp
    assert wp.looks_like_hot_browsing("最近有什么大事") is True
    assert wp.looks_like_hot_browsing("最近有什么热点") is True
    assert wp.looks_like_hot_browsing("现在大家在讨论什么") is True
    # 具体事件不算浏览型 → 应走关键词检索
    assert wp.looks_like_hot_browsing("湖南四岁幼童事件怎么样了") is False
    assert wp.looks_like_hot_browsing("那个案件的进展") is False


def test_planner_cannot_drop_hotboard_provider():
    """planner 不认识 hotboard 会把 provider 清空——规则强制项必须回填。

    生产实测：intent=hot_browsing 但 provider='' 导致热榜不被调用。
    """
    from app.chat.response_plan import build_rule_plan, apply_rule_requirements, ResponsePlan
    hint = build_rule_plan("最近有什么大事", is_owner=True)
    assert hint.provider == "hotboard"
    planned = ResponsePlan(mode="retrieve_then_answer", intent="hot_browsing",
                           provider="", confidence=0.85, source="llm")
    merged = apply_rule_requirements(planned, hint, is_owner=True)
    assert merged.provider == "hotboard"
    assert merged.tool_required is True


def test_hotboard_is_allowed_provider():
    """hotboard 必须在允许清单，否则 validate_plan 会剥掉它。"""
    from app.chat.response_plan import ResponsePlan, validate_plan
    plan = ResponsePlan(mode="retrieve_then_answer", intent="hot_browsing",
                        provider="hotboard", tool_required=True)
    assert validate_plan(plan, is_owner=True).provider == "hotboard"
