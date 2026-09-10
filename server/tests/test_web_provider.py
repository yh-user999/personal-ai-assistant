"""实时检索 provider 测试：全部用 fake HTTP，不访问真实搜索后端。"""
import asyncio
import json

import pytest

from app.chat import web_provider
from app.config import settings


class _FakeResponse:
    def __init__(self, *, payload=None, text="", status=200, content_type="application/json"):
        self._payload = payload
        self._text = text
        self.status_code = status
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise web_provider.httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self):
        if self._payload is None:
            raise json.JSONDecodeError("bad", "", 0)
        return self._payload

    @property
    def text(self):
        return self._text


class _FakeClient:
    """可编程的 httpx.AsyncClient 替身。"""

    behavior = None

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        return type(self).behavior(url, kwargs)


@pytest.fixture(autouse=True)
def backend(monkeypatch):
    monkeypatch.setattr(settings, "search_backend_url", "http://127.0.0.1:8888")
    monkeypatch.setattr(settings, "search_timeout", 5.0)
    monkeypatch.setattr(settings, "search_max_results", 10)
    yield


@pytest.fixture
def fake_http(monkeypatch):
    def install(handler):
        _FakeClient.behavior = staticmethod(handler)
        monkeypatch.setattr(web_provider.httpx, "AsyncClient", _FakeClient)
        return handler

    return install


# ── 来源与时间缺失：不进事实层 ──────────────────────────────

def test_normalize_result_requires_title_and_url():
    assert web_provider.normalize_result({"title": "", "url": "https://a.com/1"}) is None
    assert web_provider.normalize_result({"title": "T", "url": ""}) is None
    assert web_provider.normalize_result("不是对象") is None


def test_normalize_result_keeps_backend_published_time():
    item = web_provider.normalize_result({
        "title": "T", "url": "https://a.com/1", "source": "媒体",
        "publishedDate": "2026-09-10T00:00:00+00:00",
    })
    assert item["source"] == "媒体"
    assert item["published_at"].startswith("2026-09-10")
    assert item["time_known"] is True


def test_normalize_result_infers_time_from_news_url():
    """Bing News 不给 publishedDate，但新闻 URL 普遍带日期。"""
    item = web_provider.normalize_result({
        "title": "T",
        "url": "https://news.sina.com.cn/zx/gj/2026-09-10/doc-iniriaey.shtml",
        "source": "news.sina.com.cn",
    })
    assert item["published_at"] == "2026-09-10"
    assert item["time_known"] is False


def test_normalize_result_marks_unknown_time_instead_of_dropping():
    item = web_provider.normalize_result({
        "title": "T", "url": "https://a.com/article/1", "source": "媒体",
    })
    assert item is not None
    assert item["published_at"] == ""
    assert item["time_known"] is False


def test_normalize_result_falls_back_to_host_as_source():
    item = web_provider.normalize_result({
        "title": "T", "url": "https://news.example.com/1",
        "publishedDate": "2026-09-10T00:00:00+00:00",
    })
    assert item["source"] == "news.example.com"


# ── 抓页 SSRF 护栏 ──────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/secret",
    "http://localhost/admin",
    "http://192.168.1.10/router",
    "http://10.0.0.5/internal",
    "http://[::1]/x",
    "http://foo.local/x",
    "file:///etc/passwd",
    "javascript:alert(1)",
    "",
])
def test_unsafe_urls_rejected(url):
    assert web_provider._is_safe_url(url) is False


def test_public_url_allowed():
    assert web_provider._is_safe_url("https://news.example.com/a") is True
    assert web_provider._is_safe_url("http://8.8.8.8/a") is True


def test_fetch_page_blocks_private_target(fake_http):
    def handler(url, kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("内网地址不得发起请求")

    fake_http(handler)
    assert asyncio.run(web_provider.fetch_page("http://127.0.0.1:8000/")) is None


# ── 文本抽取 ────────────────────────────────────────────────

def test_extract_text_strips_script_and_tags():
    markup = "<html><script>var a=1;</script><style>.x{}</style><p>正文&amp;内容</p></html>"
    text = web_provider.extract_text(markup)
    assert "var a=1" not in text
    assert ".x{}" not in text
    assert "正文&内容" in text


# ── 检索调用与降级 ──────────────────────────────────────────

def test_search_returns_empty_when_backend_missing(monkeypatch):
    monkeypatch.setattr(settings, "search_backend_url", "")
    assert asyncio.run(web_provider.web_search("最近新闻")) == []
    assert web_provider.configured() is False


def test_search_returns_empty_on_http_error(fake_http):
    fake_http(lambda url, kwargs: _FakeResponse(status=503))
    assert asyncio.run(web_provider.web_search("最近新闻")) == []


def test_search_returns_empty_on_bad_json(fake_http):
    fake_http(lambda url, kwargs: _FakeResponse(payload=None))
    assert asyncio.run(web_provider.web_search("最近新闻")) == []


def test_search_normalizes_and_drops_malformed(fake_http):
    fake_http(lambda url, kwargs: _FakeResponse(payload={"results": [
        {"title": "有来源", "url": "https://a.com/1", "source": "媒体A",
         "publishedDate": "2026-09-10T00:00:00+00:00"},
        {"title": "时间未知", "url": "https://a.com/2", "source": "媒体B"},
        "不是对象",
    ]}))
    results = asyncio.run(web_provider.web_search("最近新闻"))
    assert [r["title"] for r in results] == ["有来源", "时间未知"]
    assert results[1]["published_at"] == ""


def test_search_passes_time_range_and_category(fake_http):
    captured = {}

    def handler(url, kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params", {})
        return _FakeResponse(payload={"results": []})

    fake_http(handler)
    asyncio.run(web_provider.web_search("x", category="news", time_range="day"))
    assert captured["params"]["time_range"] == "day"
    assert captured["params"]["categories"] == "news"
    assert captured["params"]["format"] == "json"


def test_search_invalid_time_range_falls_back_to_week(fake_http):
    captured = {}

    def handler(url, kwargs):
        captured["params"] = kwargs.get("params", {})
        return _FakeResponse(payload={"results": []})

    fake_http(handler)
    asyncio.run(web_provider.web_search("x", time_range="bogus"))
    assert captured["params"]["time_range"] == "week"


def test_search_respects_max_results_cap(fake_http, monkeypatch):
    monkeypatch.setattr(settings, "search_max_results", 2)
    fake_http(lambda url, kwargs: _FakeResponse(payload={"results": [
        {"title": f"T{i}", "url": f"https://a.com/{i}", "source": "S",
         "publishedDate": "2026-09-10T00:00:00+00:00"} for i in range(5)
    ]}))
    assert len(asyncio.run(web_provider.web_search("x"))) == 2


# ── 事件聚合 ────────────────────────────────────────────────

def _article(title, source="媒体", published="2026-09-10T00:00:00+00:00"):
    return {"title": title, "url": f"https://a.com/{abs(hash(title))}",
            "source": source, "published_at": published, "summary": ""}


def test_cluster_merges_same_event_reports():
    articles = [
        _article("湖南四岁幼童走失事件"),
        _article("湖南四岁幼童走失事件最新进展", source="媒体B"),
        _article("湖南四岁幼童走失事件后续回应", source="媒体C", published="2026-09-12T00:00:00+00:00"),
    ]
    events = web_provider.cluster_events(articles)
    assert len(events) == 1
    assert events[0]["report_count"] == 3
    assert set(events[0]["sources"]) == {"媒体", "媒体B", "媒体C"}
    assert events[0]["published_at"].startswith("2026-09-10")


def test_cluster_keeps_distinct_events_separate():
    articles = [
        _article("湖南四岁幼童走失事件"),
        _article("广东暴雨导致道路中断", source="媒体B"),
    ]
    assert len(web_provider.cluster_events(articles)) == 2


def test_cluster_does_not_merge_far_apart_in_time():
    articles = [
        _article("湖南四岁幼童走失事件"),
        _article("湖南四岁幼童走失事件", source="媒体B", published="2026-11-01T00:00:00+00:00"),
    ]
    assert len(web_provider.cluster_events(articles)) == 2


def test_cluster_orders_by_first_report():
    articles = [
        _article("事件丙", published="2026-09-12T00:00:00+00:00"),
        _article("事件甲", published="2026-09-01T00:00:00+00:00"),
    ]
    events = web_provider.cluster_events(articles)
    assert events[0]["title"] == "事件甲"


# ── 注入格式与时效判定 ──────────────────────────────────────

def test_format_sources_skips_incomplete():
    text = web_provider.format_sources([
        {"title": "T", "source": "媒体", "published_at": "2026-09-10", "summary": "摘要"},
        {"title": "缺来源", "source": "", "published_at": "2026-09-10"},
    ])
    assert "T" in text and "媒体" in text
    assert "缺来源" not in text


@pytest.mark.parametrize("text,expected", [
    ("最近有什么新闻", True),
    ("最新的进展", True),
    ("今天几号", True),
    ("帮我写个函数", False),
])
def test_freshness_intent(text, expected):
    assert web_provider.has_freshness_intent(text) is expected


def test_event_query_detection():
    assert web_provider.looks_like_event_query("湖南四岁幼童事件") is True
    assert web_provider.looks_like_event_query("最近有什么 AI 新闻") is False
    assert web_provider.looks_like_event_query("怎么写代码") is False


@pytest.mark.parametrize("text,expected", [
    ("最近有什么新闻", True),
    ("最新消息", True),
    ("湖南四岁幼童事件", True),
    ("最近AI新闻", True),
    # 自指类不得误触发联网
    ("最近的进展", False),
    ("最近项目进展怎么样", False),
    ("什么是新闻", False),
    ("今天几号", False),
])
def test_needs_web_search_boundaries(text, expected):
    assert web_provider.needs_web_search(text) is expected


def test_format_events_groups_into_single_line():
    events = web_provider.cluster_events([
        _article("湖南四岁幼童走失事件"),
        _article("湖南四岁幼童走失事件进展", source="媒体B"),
    ])
    text = web_provider.format_events(events)
    assert text.count("事件：") == 1
    assert "共 2 篇报道" in text
    assert "媒体" in text and "媒体B" in text


def test_search_and_cluster_reports_no_sources(fake_http):
    fake_http(lambda url, kwargs: _FakeResponse(payload={"results": []}))
    data = asyncio.run(web_provider.search_and_cluster("最近新闻"))
    assert data["has_sources"] is False
    assert data["results"] == [] and data["events"] == []


def test_format_sources_marks_unknown_and_inferred_time():
    text = web_provider.format_sources([
        {"title": "有时间", "source": "媒体A", "published_at": "2026-09-10", "time_known": True},
        {"title": "推断时间", "source": "媒体B", "published_at": "2026-09-09", "time_known": False},
        {"title": "无时间", "source": "媒体C", "published_at": "", "time_known": False},
    ])
    assert "（媒体A，2026-09-10）" in text
    assert "2026-09-09（据链接推断）" in text
    assert "时间未见标注" in text


# ── 空结果兜底：上游引擎会随机 CAPTCHA/超时 ─────────────────

def test_insufficient_results_widen_progressively(fake_http):
    """命中不足达标线时逐级放宽：先去时间窗，再换通用类。

    旧逻辑只在**零结果**时兜底，实测整句问法回 2 条就不放宽了，
    而同一事件实际有 52 条。
    """
    calls = []

    def handler(url, kwargs):
        params = kwargs.get("params", {})
        calls.append(params)
        # 前两次都不足 3 条，第三次给足
        if len(calls) < 3:
            return _FakeResponse(payload={"results": [
                {"title": f"不足{i}", "url": f"https://a.com/{i}", "source": "S",
                 "publishedDate": "2026-09-10T00:00:00+00:00"} for i in range(1)
            ]})
        return _FakeResponse(payload={"results": [
            {"title": f"命中{i}", "url": f"https://a.com/x{i}", "source": "S",
             "publishedDate": "2026-09-10T00:00:00+00:00"} for i in range(5)
        ]})

    fake_http(handler)
    data = asyncio.run(web_provider.search_and_cluster("最近有什么新闻"))

    assert len(calls) == 3
    assert calls[0].get("categories") == "news" and calls[0]["time_range"] == "week"
    assert calls[1].get("categories") == "news" and "time_range" not in calls[1]  # 去时间窗
    assert calls[2].get("categories") is None and "time_range" not in calls[2]    # 通用类
    assert data["has_sources"] is True
    assert data["result_count"] == 5
    assert data["fallback_used"] is True
    assert data["attempts"] == 3


def test_sufficient_results_stop_after_first_attempt(fake_http):
    """首次就达标 → 只发一次请求，正常路径无额外开销。"""
    calls = []

    def handler(url, kwargs):
        calls.append(kwargs.get("params", {}))
        return _FakeResponse(payload={"results": [
            {"title": f"命中{i}", "url": f"https://a.com/{i}", "source": "S",
             "publishedDate": "2026-09-10T00:00:00+00:00"} for i in range(5)
        ]})

    fake_http(handler)
    data = asyncio.run(web_provider.search_and_cluster("科技新闻"))

    assert len(calls) == 1
    assert data["fallback_used"] is False
    assert data["attempts"] == 1


def test_all_attempts_exhausted_reports_no_sources(fake_http):
    fake_http(lambda url, kwargs: _FakeResponse(payload={"results": []}))
    data = asyncio.run(web_provider.search_and_cluster("最近有什么新闻"))
    assert data["has_sources"] is False
    assert data["result_count"] == 0


@pytest.mark.parametrize("raw,expected", [
    ("你知道最近湖南四岁幼童事件吗", "湖南四岁幼童事件"),
    ("请问今天有什么新闻", "新闻"),
    ("帮我查查具身智能的报道", "具身智能的报道"),
    ("湖南四岁幼童事件", "湖南四岁幼童事件"),
])
def test_clean_query_strips_conversational_frames(raw, expected):
    assert web_provider.clean_query(raw) == expected


def test_clean_query_keeps_content_words():
    """剥包装不能伤到实体词——"事件/新闻/幼童"是检索关键。"""
    for word in ("事件", "新闻", "幼童", "科技"):
        assert word in web_provider.clean_query(f"你知道最近{word}吗")


def test_clean_query_falls_back_when_too_short():
    """剥完太空就退回原文，宁可多几个词也不要搜空。"""
    assert web_provider.clean_query("请问吗") == "请问吗"


@pytest.mark.parametrize("start,expected", [
    ("day", "week"), ("week", "month"), ("month", "year"), ("year", "year"),
])
def test_widen_time_range(start, expected):
    assert web_provider.widen_time_range(start) == expected
