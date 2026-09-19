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
    assert plan.queries[0] == "没钱修什么仙？ 作品简介 剧情 设定"
    assert plan.max_rounds == 2
    assert plan.max_pages == 3


def test_research_reads_pages_and_builds_evidence(monkeypatch):
    async def fake_search(*args, **kwargs):
        return {
            "results": [
                {
                    "title": "没钱修什么仙？公开资料",
                    "url": "https://example.com/book",
                    "source": "example.com",
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
    assert result.sources[0]["url"] == "https://example.com/book"
    assert "张羽" in result.sources[0]["text"]
    assert "来源链接：https://example.com/book" in result.prompt_block()
    assert result.evidence["sources"][0]["id"].startswith("web_")


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
