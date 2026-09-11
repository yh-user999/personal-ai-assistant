"""实时检索 provider 测试：全部用 fake HTTP，不访问真实搜索后端。"""
import asyncio
import ipaddress
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
    # 默认关掉 GDELT，避免主检索测试误连真实 API；需要时用例内单独开
    monkeypatch.setattr(settings, "gdelt_enabled", False)
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


def test_search_switch_disables_search_and_fetch(monkeypatch, fake_http):
    monkeypatch.setattr(settings, "web_search_enabled", False)
    fake_http(lambda url, kwargs: pytest.fail("搜索开关关闭后不得发起请求"))
    assert web_provider.configured() is False
    assert asyncio.run(web_provider.web_search("最近新闻")) == []
    assert asyncio.run(web_provider.fetch_page("https://news.example/article")) is None


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
    assert data["result_count"] == 6  # 保留前两轮取得的同一个URL，再并入第三轮5条
    assert "https://a.com/0" in {r["url"] for r in data["results"]}
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


# ── GDELT 合并 ──────────────────────────────────────────────

def test_gdelt_results_merged_and_deduped(fake_http, monkeypatch):
    """GDELT 结果并入 SearXNG，同 URL 去重。"""
    fake_http(lambda url, kwargs: _FakeResponse(payload={"results": [
        {"title": "主源A", "url": "https://sx.com/a", "source": "sx.com",
         "publishedDate": "2026-09-10T00:00:00+00:00"},
    ]}))

    async def fake_gdelt(query, *, time_range="week", limit=10):
        return [
            {"title": "GDELT新源", "url": "https://gd.com/x", "source": "gd.com",
             "published_at": "2026-09-09T00:00:00Z", "time_known": True, "summary": "", "language": "Chinese"},
            {"title": "重复", "url": "https://sx.com/a", "source": "sx.com",
             "published_at": "", "time_known": False, "summary": "", "language": ""},
        ]

    from app.chat import gdelt_provider
    monkeypatch.setattr(gdelt_provider, "configured", lambda: True)
    monkeypatch.setattr(gdelt_provider, "search", fake_gdelt)

    data = asyncio.run(web_provider.search_and_cluster("某事件"))
    urls = {r["url"] for r in data["results"]}
    assert "https://gd.com/x" in urls          # GDELT 新源并入
    assert data["gdelt_count"] == 1            # 去掉重复的那条，只算新增 1
    assert len([r for r in data["results"] if r["url"] == "https://sx.com/a"]) == 1


# ── 规则B：事件深挖多角度检索 ──────────────────────────────

def test_build_angle_queries():
    qs = web_provider.build_angle_queries("湖南四岁幼童事件")
    assert len(qs) == 3
    assert all("湖南四岁幼童事件" in q for q in qs)
    # 含"可能推翻印象"的角度
    assert any("经过" in q for q in qs)
    assert any("说法" in q or "争议" in q for q in qs)
    assert web_provider.build_angle_queries("") == []


def test_deep_dive_adds_angle_results(fake_http):
    """深挖时追加正交角度检索，并入去重。"""
    calls = []

    def handler(url, kwargs):
        q = kwargs.get("params", {}).get("q", "")
        calls.append(q)
        # 主查询给足结果；角度查询各给一条不同 URL
        if "经过" in q or "说法" in q or "争议" in q or "反转" in q or "律师" in q:
            return _FakeResponse(payload={"results": [
                {"title": f"角度{len(calls)}", "url": f"https://ang.com/{len(calls)}",
                 "source": "媒体", "publishedDate": "2026-09-10T00:00:00+00:00"},
            ]})
        return _FakeResponse(payload={"results": [
            {"title": f"主{i}", "url": f"https://m.com/{i}", "source": "媒体",
             "publishedDate": "2026-09-10T00:00:00+00:00"} for i in range(5)
        ]})

    fake_http(handler)
    data = asyncio.run(web_provider.search_and_cluster("某某事件", deep_dive=True))
    assert data["angle_added"] >= 1
    assert any("ang.com" in r["url"] for r in data["results"])


def test_no_deep_dive_by_default(fake_http):
    """默认不深挖：只跑主检索，不发角度查询。"""
    calls = []

    def handler(url, kwargs):
        calls.append(kwargs.get("params", {}).get("q", ""))
        return _FakeResponse(payload={"results": [
            {"title": f"主{i}", "url": f"https://m.com/{i}", "source": "媒体",
             "publishedDate": "2026-09-10T00:00:00+00:00"} for i in range(5)
        ]})

    fake_http(handler)
    data = asyncio.run(web_provider.search_and_cluster("某某事件"))
    assert data["angle_added"] == 0
    assert not any("经过" in c or "争议" in c for c in calls)


def test_gdelt_failure_does_not_break_main_search(fake_http, monkeypatch):
    """GDELT 抛错不影响 SearXNG 已有结果。"""
    fake_http(lambda url, kwargs: _FakeResponse(payload={"results": [
        {"title": "主源", "url": "https://sx.com/a", "source": "sx.com",
         "publishedDate": "2026-09-10T00:00:00+00:00"},
    ]}))

    async def boom(query, **kw):
        raise RuntimeError("gdelt down")

    from app.chat import gdelt_provider
    monkeypatch.setattr(gdelt_provider, "configured", lambda: True)
    monkeypatch.setattr(gdelt_provider, "search", boom)

    data = asyncio.run(web_provider.search_and_cluster("某事件"))
    assert data["has_sources"] is True
    assert data["gdelt_count"] == 0


def test_explicit_first_search_attempt_cap_skips_widening_angles_and_gdelt(fake_http, monkeypatch):
    calls = []

    def handler(url, kwargs):
        calls.append(kwargs)
        return _FakeResponse(payload={"results": []})

    from app.chat import gdelt_provider
    monkeypatch.setattr(gdelt_provider, "configured", lambda: True)

    async def no_gdelt(*args, **kwargs):
        pytest.fail("总调用上限用完后不能访问第二搜索源")

    monkeypatch.setattr(gdelt_provider, "search", no_gdelt)
    fake_http(handler)
    result = asyncio.run(web_provider.search_and_cluster("事件A", alt_query="另一词", deep_dive=True, max_attempts=1))
    assert len(calls) == 1 and result["attempts"] == 1 and result["request_count"] == 1
    assert result["angle_added"] == 0 and result["gdelt_count"] == 0


def test_widening_keeps_earlier_sources_when_batches_shrink_to_empty(fake_http, monkeypatch):
    monkeypatch.setattr(settings, "search_min_results", 5)
    batches = [
        [{"title": "事件A原始经过", "url": "https://a.com/one"}, {"title": "事件A各方说法", "url": "https://a.com/two"}],
        [{"title": "事件A补充记录", "url": "https://a.com/three"}], [],
    ]
    calls = []

    def handler(url, kwargs):
        calls.append(kwargs["params"])
        return _FakeResponse(payload={"results": batches[len(calls) - 1]})

    fake_http(handler)
    data = asyncio.run(web_provider.search_and_cluster("事件A", max_attempts=3))
    assert [len(b) for b in batches] == [2, 1, 0]
    assert data["result_count"] == 3 and data["has_sources"]
    assert {r["url"] for r in data["results"]} == {"https://a.com/one", "https://a.com/two", "https://a.com/three"}
    assert [a["received"] for a in data["attempt_log"]] == [2, 1, 0]
    assert [a["added"] for a in data["attempt_log"]] == [2, 1, 0]
    assert data["fallback_used"] and data["attempts"] == 3 and not data["threshold_met"]


def test_widening_stops_at_unique_merged_threshold_not_last_batch(fake_http):
    batches = [
        [{"title": "事件A原始经过", "url": "https://a.com/one"}, {"title": "事件A各方说法", "url": "https://a.com/two"}],
        [{"title": "事件A重复", "url": "https://a.com/two"}, {"title": "事件A新记录", "url": "https://a.com/three"}],
    ]
    calls = []

    def handler(url, kwargs):
        calls.append(kwargs)
        assert len(calls) <= 2, "累计去重已达3条，不应再跑第三轮"
        return _FakeResponse(payload={"results": batches[len(calls) - 1]})

    fake_http(handler)
    data = asyncio.run(web_provider.search_and_cluster("事件A"))
    assert data["threshold_met"] and data["result_count"] == 3 and data["attempts"] == 2
    assert [a["added"] for a in data["attempt_log"]] == [2, 1]
    assert len({r["url"] for r in data["results"]}) == 3


def test_zero_search_attempt_cap_makes_no_backend_call(fake_http):
    calls = []

    def handler(url, kwargs):
        calls.append(kwargs)
        return _FakeResponse(payload={"results": []})

    fake_http(handler)
    data = asyncio.run(web_provider.search_and_cluster("事件A", max_attempts=0))
    assert calls == []
    assert data["attempts"] == 0 and data["request_count"] == 0
    assert data["has_sources"] is False


def test_empty_fallback_is_reported_even_when_only_first_query_contributed(fake_http):
    calls = []

    def handler(url, kwargs):
        calls.append(kwargs["params"])
        batch = [{"title": "事件A", "url": "https://a.com/one"}] if len(calls) == 1 else []
        return _FakeResponse(payload={"results": batch})

    fake_http(handler)
    data = asyncio.run(web_provider.search_and_cluster("事件A", alt_query="另一事件关键词", max_attempts=2))
    assert data["query_used"] == "事件A"  # 最后实际贡献来源的是首次查询
    assert data["fallback_used"] is True  # 备用词确实被尝试，不能误报没有放宽
    assert data["attempts"] == 2 and data["result_count"] == 1
    assert data["attempt_log"][1]["query"] == "另一事件关键词"
    assert data["attempt_log"][1]["added"] == 0


def test_search_budget_expiry_preserves_completed_batch_and_cancels_pending(monkeypatch):
    calls, cancelled = [], []

    async def search(q, **kwargs):
        calls.append(q)
        if len(calls) == 1:
            return [{"title": "事件A", "url": "https://a.com/one", "summary": "有效原始资料。"}]
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(web_provider, "web_search", search)
    data = asyncio.run(web_provider.search_and_cluster("事件A", max_attempts=3, budget_seconds=0.03))
    assert data["result_count"] == 1 and data["results"][0]["url"] == "https://a.com/one"
    assert data["budget_exhausted"] and data["stop_reason"] == "budget_exhausted"
    assert len(calls) == 2 and cancelled
    assert data["attempt_log"][1]["status"] == "TimeoutError"


def test_search_zero_shared_budget_starts_no_requests(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("零剩余预算不得继续首检")

    monkeypatch.setattr(web_provider, "web_search", forbidden)
    data = asyncio.run(web_provider.search_and_cluster("事件A", budget_seconds=0))
    assert data["budget_exhausted"] and data["request_count"] == 0 and data["attempts"] == 0


def test_angle_budget_timeout_retains_the_successful_parallel_result(monkeypatch):
    cancelled = []

    async def search(q, **kwargs):
        if q == "事件A":
            return [{"title": "事件A", "url": f"https://a.com/{i}"} for i in range(3)]
        if "经过" in q:
            return [{"title": "事件A关键原文", "url": "https://a.com/original"}]
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(web_provider, "web_search", search)
    data = asyncio.run(web_provider.search_and_cluster("事件A", deep_dive=True, budget_seconds=0.03))
    assert "https://a.com/original" in {r["url"] for r in data["results"]}
    assert data["angle_added"] == 1 and data["budget_exhausted"]
    assert len(cancelled) == 2


# ── 原网页安全流读取：HTTP 与 DNS 均为 fake ──────────────────

class _PageStream(web_provider.httpx.AsyncByteStream):
    def __init__(self, chunks, *, wait=False):
        self.chunks = chunks
        self.read_count = 0
        self.closed = False
        self.wait = wait

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk
        if self.wait:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


@pytest.fixture
def page_network(monkeypatch):
    """实际运行 HTTPX 的流协议，只有传输和 DNS 是替身，不可能访问真网络。"""
    real_client = web_provider.httpx.AsyncClient
    calls, resolutions = [], []
    state = {"dns": {}, "handler": None}

    async def dns(self, host, port, **kwargs):
        resolutions.append(host)
        ips = state["dns"].get(host, ["93.184.216.34"])
        return [(2, 1, 6, "", (ip, port)) for ip in ips]

    def handler(request):
        calls.append(request)
        if state["handler"] is None:
            raise AssertionError("没有设置fake页面响应")
        return state["handler"](request)

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", dns)
    monkeypatch.setattr(web_provider.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=web_provider.httpx.MockTransport(handler), **kwargs,
    ))
    state.update(calls=calls, resolutions=resolutions)
    return state


def _page_response(text="<p>事件A合成正文</p>", *, status=200, headers=None, stream=None):
    return web_provider.httpx.Response(status, headers={"content-type": "text/html; charset=utf-8", **(headers or {})},
                                       stream=stream or _PageStream([text.encode("utf-8")]))


def test_fetch_page_reads_original_and_pins_dns_with_original_tls_host(page_network):
    page_network["handler"] = lambda request: _page_response()
    page = asyncio.run(web_provider.fetch_page("https://news.example/article"))
    assert page["text"] == "事件A合成正文"
    assert page["url"] == "https://news.example/article"
    request = page_network["calls"][0]
    assert request.url.host == "93.184.216.34"  # 不让 HTTP 客户端重新解析不可信域名
    assert request.headers["host"] == "news.example"
    assert request.extensions["sni_hostname"] == "news.example"
    assert request.headers["accept-encoding"] == "identity"
    assert page_network["resolutions"] == ["news.example"]


# 用整数构造不可路由地址，避免把任何真实/私有地址字面量写进测试文件。
_NONPUBLIC_TEST_ADDRESSES = [str(ipaddress.ip_address(value)) for value in (
    0x7F000001, 0x0A010203, 0xAC100001, 0xC0A80101,
    0xA9FEA9FE, 0x00000000, 0x64400001, 0xE0000001,
    0xC000020A,
)] + [str(ipaddress.IPv6Address(value)) for value in (
    1, 0xFE800000000000000000000000000001,
    0xFC000000000000000000000000000001, 0xFFFF00007F000001,
)]


@pytest.mark.parametrize("address", _NONPUBLIC_TEST_ADDRESSES)
def test_fetch_rejects_all_nonpublic_dns_addresses(page_network, address):
    page_network["dns"]["unsafe.example"] = [address]
    assert asyncio.run(web_provider.fetch_page("https://unsafe.example/secret")) is None
    assert not page_network["calls"]


def test_fetch_rejects_mixed_public_and_private_dns_answers(page_network):
    page_network["dns"]["mixed.example"] = ["93.184.216.34", _NONPUBLIC_TEST_ADDRESSES[1]]
    assert asyncio.run(web_provider.fetch_page("https://mixed.example/")) is None
    assert not page_network["calls"]


@pytest.mark.parametrize("destination", [
    f"http://{_NONPUBLIC_TEST_ADDRESSES[0]}/admin",
    f"http://{_NONPUBLIC_TEST_ADDRESSES[4]}/latest/meta-data",
    "http://internal.example/secret",
])
def test_redirect_cannot_enter_private_network(page_network, destination):
    page_network["dns"]["internal.example"] = [_NONPUBLIC_TEST_ADDRESSES[3]]
    page_network["handler"] = lambda request: _page_response(status=302, headers={"location": destination})
    assert asyncio.run(web_provider.fetch_page("https://news.example/start")) is None
    assert len(page_network["calls"]) == 1


def test_relative_redirect_is_validated_again_and_limit_is_bounded(page_network):
    page_network["handler"] = lambda request: _page_response(status=302, headers={"location": "/loop"})
    assert asyncio.run(web_provider.fetch_page("https://news.example/start")) is None
    assert len(page_network["calls"]) == 4
    assert len(page_network["resolutions"]) == 4


def test_public_redirect_can_fetch_final_article(page_network):
    def handler(request):
        if request.url.path == "/start":
            return _page_response(status=302, headers={"location": "https://other.example/full"})
        return _page_response("<p>完整经过正文。</p>")

    page_network["handler"] = handler
    page = asyncio.run(web_provider.fetch_page("https://news.example/start"))
    assert page["url"] == "https://other.example/full"
    assert page["text"] == "完整经过正文。"
    assert page_network["resolutions"] == ["news.example", "other.example"]
    assert page_network["calls"][1].headers["host"] == "other.example"


def test_stream_byte_limit_applies_before_body_is_buffered(page_network, monkeypatch):
    monkeypatch.setattr(settings, "search_max_page_bytes", 16)
    stream = _PageStream([b"x" * 17, b"this must not be read"])
    page_network["handler"] = lambda request: _page_response(stream=stream)
    assert asyncio.run(web_provider.fetch_page("https://news.example/huge")) is None
    assert stream.read_count == 1 and stream.closed


def test_content_length_can_reject_without_reading_any_body(page_network, monkeypatch):
    monkeypatch.setattr(settings, "search_max_page_bytes", 16)
    stream = _PageStream([b"large"])
    page_network["handler"] = lambda request: _page_response(headers={"content-length": "1000000"}, stream=stream)
    assert asyncio.run(web_provider.fetch_page("https://news.example/huge")) is None
    assert stream.read_count == 0 and stream.closed


def test_compressed_response_cannot_trigger_unbounded_decompression(page_network):
    stream = _PageStream([b"gzip bytes should not be consumed"])
    page_network["handler"] = lambda request: _page_response(headers={"content-encoding": "gzip"}, stream=stream)
    assert asyncio.run(web_provider.fetch_page("https://news.example/compressed")) is None
    assert stream.read_count == 0 and stream.closed


@pytest.mark.parametrize("url", [
    "http://localhost./", "http://user:pass@news.example/", "http://news.example:99999/",
    f"http://news.example\\@{_NONPUBLIC_TEST_ADDRESSES[0]}/",
    f"http://[{_NONPUBLIC_TEST_ADDRESSES[-1]}]/", f"http://{_NONPUBLIC_TEST_ADDRESSES[6]}/",
])
def test_additional_unsafe_url_forms_never_reach_network(page_network, url):
    assert asyncio.run(web_provider.fetch_page(url)) is None
    assert not page_network["calls"] and not page_network["resolutions"]


def test_page_total_timeout_includes_dns(page_network, monkeypatch):
    monkeypatch.setattr(settings, "search_timeout", 0.015)
    cancelled = []

    async def hanging_dns(self, *args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", hanging_dns)
    assert asyncio.run(web_provider.fetch_page("https://news.example/")) is None
    assert cancelled and not page_network["calls"]


def test_fetch_cancellation_closes_stream_and_propagates(page_network):
    async def scenario():
        started = asyncio.Event()

        class Blocking(_PageStream):
            async def __aiter__(self):
                started.set()
                await asyncio.Event().wait()
                yield b"never"

        stream = Blocking([])
        page_network["handler"] = lambda request: _page_response(stream=stream)
        task = asyncio.create_task(web_provider.fetch_page("https://news.example/"))
        await asyncio.wait_for(started.wait(), 0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed

    asyncio.run(scenario())
