import asyncio
import base64
from types import SimpleNamespace

from app.chat import github_provider


def _settings():
    return SimpleNamespace(
        github_api_base="https://api.github.com",
        github_token="",
        github_timeout=5.0,
        github_max_results=6,
    )


def test_github_query_detection():
    assert github_provider.looks_like_project_lookup("搜索某个 GitHub 项目")
    assert github_provider.looks_like_project_lookup("这个仓库最近的 release")
    assert not github_provider.looks_like_project_lookup("你好")
    assert github_provider.rewrite_project_query("搜索 Crawl4AI GitHub 项目最近的 release") == "Crawl4AI"


def test_github_repo_url_returns_repository_readme_and_releases(monkeypatch):
    monkeypatch.setattr(github_provider, "_settings", _settings)

    async def fake_get(client, path, params=None):
        if path == "repos/acme/demo":
            return {
                "full_name": "acme/demo",
                "html_url": "https://github.com/acme/demo",
                "description": "一个示例项目",
                "language": "Python",
                "stargazers_count": 12,
                "updated_at": "2026-09-18T00:00:00Z",
            }
        if path.endswith("/readme"):
            return {"content": base64.b64encode("# Demo\n用法说明".encode()).decode()}
        if path.endswith("/releases"):
            return [{
                "html_url": "https://github.com/acme/demo/releases/tag/v1.0.0",
                "name": "v1.0.0",
                "body": "首个版本",
                "published_at": "2026-09-17T00:00:00Z",
            }]
        raise AssertionError(path)

    monkeypatch.setattr(github_provider, "_get", fake_get)
    results = asyncio.run(github_provider.search("https://github.com/acme/demo"))

    assert [item["kind"] for item in results] == ["repository", "readme", "release"]
    assert results[0]["url"] == "https://github.com/acme/demo"
    assert "用法说明" in results[1]["summary"]
    assert results[2]["url"].endswith("v1.0.0")


def test_github_repository_search_and_issue_search(monkeypatch):
    monkeypatch.setattr(github_provider, "_settings", _settings)

    async def fake_get(client, path, params=None):
        if path == "search/repositories":
            return {"items": [{
                "full_name": "acme/assistant",
                "html_url": "https://github.com/acme/assistant",
                "description": "个人助手",
                "language": "Python",
                "stargazers_count": 3,
                "updated_at": "2026-09-19T00:00:00Z",
            }]}
        if path == "search/issues":
            return {"items": [{
                "html_url": "https://github.com/acme/assistant/issues/1",
                "title": "支持网页研究",
                "body": "希望增加来源引用",
                "updated_at": "2026-09-19T00:00:00Z",
            }]}
        raise AssertionError(path)

    monkeypatch.setattr(github_provider, "_get", fake_get)
    repos = asyncio.run(github_provider.search("个人 AI 助手"))
    issues = asyncio.run(github_provider.search_issues("repo:acme/assistant 网页研究"))

    assert repos[0]["title"] == "GitHub 仓库：acme/assistant"
    assert repos[0]["provider"] == "github"
    assert issues[0]["kind"] == "issue"
    assert issues[0]["url"].endswith("/issues/1")


def test_github_failures_degrade_to_empty_results(monkeypatch):
    monkeypatch.setattr(github_provider, "_settings", _settings)

    async def unavailable(*args, **kwargs):
        return None

    monkeypatch.setattr(github_provider, "_get", unavailable)
    assert asyncio.run(github_provider.search("开源个人助手")) == []
    assert asyncio.run(github_provider.search("https://github.com/acme/missing")) == []
    assert asyncio.run(github_provider.search_issues("repo:acme/missing bug")) == []
