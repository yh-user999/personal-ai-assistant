import asyncio
from types import SimpleNamespace

from app.chat import web_provider, web_research


def _settings(**overrides):
    values = {
        "web_research_budget_seconds": 10.0,
        "web_research_max_rounds": 2,
        "web_research_max_pages": 3,
        "web_research_max_results": 6,
        "search_min_results": 2,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_research_query_classification_is_conservative():
    assert web_research.classify_query("你好") == "none"
    assert web_research.classify_query("帮我查一下《没钱修什么仙》的作者") == "novel"
    assert web_research.classify_query("帮我找 Python asyncio 官方文档") == "document"
    assert web_research.classify_query("搜索 personal-ai-assistant GitHub 最近的 release") == "project"
    assert web_research.classify_query("打开 https://example.com/page 总结") == "url"
    assert web_research.classify_query("帮我搜索 TCP 三次握手资料") == "knowledge"


def test_research_plan_rewrites_novel_and_bounds_budget():
    plan = web_research.build_plan("帮我查一下《没钱修什么仙》的作者和设定", settings=_settings())
    assert plan.kind == "novel"
    assert plan.queries[0] == "没钱修什么仙？"
    assert plan.queries[1] == "没钱修什么仙？ 作品简介 剧情 设定"
    assert plan.max_rounds == 2
    assert plan.max_pages == 3


def test_semantic_plan_queries_override_rule_classification(monkeypatch):
    searched = []

    async def fake_search(query, **kwargs):
        searched.append(query)
        return {
            "results": [{
                "title": "TCP 资料",
                "url": "https://example.com/tcp",
                "source": "example.com",
                "summary": "三次握手摘要",
                "published_at": "",
            }],
            "events": [],
            "has_sources": True,
        }

    async def fake_fetch(url):
        return {"url": url, "text": "TCP 三次握手资料正文"}

    monkeypatch.setattr(web_provider, "search_and_cluster", fake_search)
    monkeypatch.setattr(web_provider, "fetch_page", fake_fetch)
    result = asyncio.run(
        web_research.run_research(
            "你知道这个吗？",
            settings=_settings(),
            semantic_plan={
                "route": "web_research",
                "research_kind": "knowledge",
                "subject": "TCP 三次握手",
                "research_question": "请核实原理",
                "research_queries": ["TCP 三次握手 RFC"],
                "source_preference": ["ietf.org"],
            },
        )
    )

    assert result.kind == "knowledge"
    assert searched == ["TCP 三次握手 RFC"]
    assert result.source_preference == ("ietf.org",)
    assert result.evidence["checks"]["source_preference"] == ["ietf.org"]


def test_research_reads_pages_and_builds_evidence(monkeypatch):
    async def fake_search(*args, **kwargs):
        return {
            "results": [
                {
                    "title": "没钱修什么仙？起点中文网",
                    "url": "https://www.qidian.com/book/1042256511/",
                    "source": "起点中文网",
                    "summary": "作者为熊狼狗，主角为张羽。",
                    "published_at": "",
                }
            ],
            "events": [],
            "has_sources": True,
        }

    async def fake_fetch(url):
        return {
            "url": url,
            "text": "作者为熊狼狗。主角张羽在修仙需要持续投入的世界中面对债务压力。",
        }

    monkeypatch.setattr(web_provider, "search_and_cluster", fake_search)
    monkeypatch.setattr(web_provider, "fetch_page", fake_fetch)

    result = asyncio.run(
        web_research.run_research(
            "帮我查一下《没钱修什么仙》的作者和设定",
            settings=_settings(),
        )
    )

    assert result.kind == "novel"
    assert result.pages_read == 1
    assert result.sources[0]["url"] == "https://www.qidian.com/book/1042256511/"
    assert "张羽" in result.sources[0]["text"]
    assert "来源链接：https://www.qidian.com/book/1042256511/" in result.prompt_block()
    assert result.evidence["sources"][0]["id"].startswith("web_")
    assert result.evidence["has_reliable_sources"] is True
    assert result.has_reliable_sources is True


def test_novel_research_drops_low_quality_sources_and_keeps_page_body(monkeypatch):
    async def fake_search(*args, **kwargs):
        return {
            "results": [
                {
                    "title": "没钱修什么仙最新章节",
                    "url": "https://example.com/chapters",
                    "source": "转载站",
                    "summary": "全文免费，最新章节目录",
                },
                {
                    "title": "没钱修什么仙？起点中文网",
                    "url": "https://www.qidian.com/book/1042256511/",
                    "source": "起点中文网",
                    "summary": "熊狼狗作品简介",
                },
            ],
            "events": [],
            "has_sources": True,
        }

    async def fake_fetch(url):
        return {
            "url": url,
            "text": "作品页正文：作者为熊狼狗，公开简介说明修仙需要持续投入。",
        }

    monkeypatch.setattr(web_provider, "search_and_cluster", fake_search)
    monkeypatch.setattr(web_provider, "fetch_page", fake_fetch)

    result = asyncio.run(
        web_research.run_research(
            "你知道《没钱修什么仙》吗？",
            settings=_settings(),
        )
    )

    assert result.has_reliable_sources is True
    assert [source["url"] for source in result.sources] == [
        "https://www.qidian.com/book/1042256511/",
    ]
    assert "作品页正文" in result.sources[0]["text"]
    assert "作品页正文" in result.prompt_block()
    assert "最新章节" not in result.prompt_block()


def test_novel_research_with_only_low_quality_sources_degrades_safely(monkeypatch):
    async def low_quality_search(*args, **kwargs):
        return {
            "results": [{
                "title": "没钱修什么仙最新章节",
                "url": "https://example.com/chapters",
                "source": "转载站",
                "summary": "全文免费，最新章节目录",
            }],
            "events": [],
            "has_sources": True,
        }

    monkeypatch.setattr(web_provider, "search_and_cluster", low_quality_search)

    result = asyncio.run(
        web_research.run_research(
            "帮我查《没钱修什么仙》的剧情",
            settings=_settings(),
        )
    )

    assert result.has_sources is False
    assert result.has_reliable_sources is False
    assert result.stop_reason == "no_sources"
    assert result.prompt_block() == ""


def test_project_release_query_rewrites_and_reads_repository_details(monkeypatch):
    async def fake_search(query, *, limit=None):
        if query == "Crawl4AI":
            return [{
                "title": "GitHub 仓库：acme/crawl4ai",
                "url": "https://github.com/acme/crawl4ai",
                "source": "github.com",
                "summary": "网页抓取项目",
                "kind": "repository",
            }]
        if query == "https://github.com/acme/crawl4ai":
            return [{
                "title": "GitHub 仓库：acme/crawl4ai",
                "url": "https://github.com/acme/crawl4ai",
                "source": "github.com",
                "summary": "网页抓取项目",
                "kind": "repository",
            }, {
                "title": "acme/crawl4ai Release：v1.0.0",
                "url": "https://github.com/acme/crawl4ai/releases/tag/v1.0.0",
                "source": "github.com",
                "summary": "首个版本",
                "kind": "release",
            }]
        raise AssertionError(query)

    async def fake_fetch(url):
        return {"url": url, "text": "GitHub 项目正文"}

    monkeypatch.setattr(web_research.github_provider, "search", fake_search)
    monkeypatch.setattr(web_provider, "fetch_page", fake_fetch)
    result = asyncio.run(
        web_research.run_research(
            "搜索 Crawl4AI GitHub 项目最近的 release",
            settings=_settings(),
        )
    )

    assert result.provider == "github"
    assert any(item["kind"] == "release" for item in result.sources)
    assert any("releases/tag/v1.0.0" in item["url"] for item in result.sources)


def test_research_failure_returns_no_sources_without_raising(monkeypatch):
    async def empty_search(*args, **kwargs):
        return {"results": [], "events": [], "has_sources": False}

    monkeypatch.setattr(web_provider, "search_and_cluster", empty_search)
    result = asyncio.run(
        web_research.run_research(
            "帮我搜索一个不存在的资料",
            settings=_settings(),
        )
    )

    assert result.has_sources is False
    assert result.stop_reason == "no_sources"
    assert result.prompt_block() == ""
    assert result.evidence["sources"] == []
