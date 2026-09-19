"""GitHub 公开项目 Provider。

只负责把 GitHub REST API 的仓库、README、Release、Issue/PR 结果规范化为
聊天检索可消费的来源结构；不负责判断用户是否需要联网，也不写入记忆。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger("assistant.github")

_REPO_URL_RE = re.compile(r"github\.com/([^/\s]+)/([^/\s#?]+)", re.IGNORECASE)
_PROJECT_RE = re.compile(
    r"github|仓库|项目|repo|repository|issue|pr|pull\s+request|release|开源",
    re.IGNORECASE,
)


def looks_like_project_lookup(text: str) -> bool:
    return bool(_PROJECT_RE.search(text or ""))


def rewrite_project_query(text: str) -> str:
    """去掉中文操作词，只把项目名/技术词交给 GitHub 搜索。"""
    value = (text or "").strip()
    if not value:
        return ""
    if _repo_from_url(value):
        return value
    stopwords = {
        "搜索", "搜一下", "搜搜", "查一下", "查找", "找一下", "帮我找", "看看",
        "GitHub", "github", "项目", "仓库", "开源", "最近", "最新", "有什么",
        "的", "一下", "资料", "信息", "介绍", "发布", "版本", "更新", "release",
        "releases", "Issue", "issue", "issues", "pull", "request", "PR", "pr",
    }
    normalized = value
    for stopword in sorted(stopwords, key=len, reverse=True):
        normalized = re.sub(re.escape(stopword), " ", normalized, flags=re.IGNORECASE)
    tokens = re.findall(r"[A-Za-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,}", normalized)
    kept = [token for token in tokens if token not in stopwords]
    return " ".join(kept)[:200] or value[:200]


def _settings() -> Any:
    from app.config import settings

    return settings


def _base_url() -> str:
    return str(getattr(_settings(), "github_api_base", "https://api.github.com") or "https://api.github.com").rstrip("/")


def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "personal-ai-assistant-web-research",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = str(getattr(_settings(), "github_token", "") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _timeout() -> float:
    try:
        value = float(getattr(_settings(), "github_timeout", 10.0))
    except (TypeError, ValueError, OverflowError):
        value = 10.0
    return max(1.0, min(value, 30.0))


def _limit(value: int | None = None) -> int:
    try:
        configured = int(getattr(_settings(), "github_max_results", 6))
    except (TypeError, ValueError, OverflowError):
        configured = 6
    requested = configured if value is None else int(value)
    return max(1, min(requested, configured, 20))


def _repo_from_url(text: str) -> tuple[str, str] | None:
    match = _REPO_URL_RE.search(text or "")
    if not match:
        return None
    owner = match.group(1).strip()
    repo = match.group(2).strip().removesuffix(".git")
    if not owner or not repo:
        return None
    return owner, repo


def _source(*, title: str, url: str, summary: str, kind: str, updated_at: str = "") -> dict[str, Any]:
    return {
        "title": title[:300],
        "url": url[:1000],
        "source": "github.com",
        "published_at": updated_at[:40],
        "time_known": bool(updated_at),
        "summary": summary[:1000],
        "language": "",
        "provider": "github",
        "kind": kind,
    }


def _decode_readme(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    encoded = payload.get("content")
    if not isinstance(encoded, str):
        return ""
    try:
        return base64.b64decode(encoded.replace("\n", ""), validate=False).decode("utf-8", errors="replace")[:12000]
    except (ValueError, TypeError):
        return ""


async def _get(client: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None) -> Any | None:
    try:
        response = await client.get(f"{_base_url()}/{path.lstrip('/')}", params=params)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.warning("GitHub 请求失败（%s）", type(exc).__name__)
        return None


def _repo_result(item: dict[str, Any]) -> dict[str, Any] | None:
    full_name = str(item.get("full_name") or "").strip()
    url = str(item.get("html_url") or "").strip()
    if not full_name or not url:
        return None
    description = str(item.get("description") or "").strip()
    language = str(item.get("language") or "").strip()
    stars = item.get("stargazers_count")
    suffix = f"；语言：{language}" if language else ""
    if isinstance(stars, int):
        suffix += f"；Stars：{stars}"
    return _source(
        title=f"GitHub 仓库：{full_name}",
        url=url,
        summary=(description or "公开 GitHub 仓库") + suffix,
        kind="repository",
        updated_at=str(item.get("updated_at") or ""),
    )


async def search(query: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    """搜索公开仓库，失败/限流时返回空列表。"""
    text = (query or "").strip()
    if not text:
        return []
    cap = _limit(limit)
    repo = _repo_from_url(text)
    async with httpx.AsyncClient(timeout=_timeout(), headers=_headers(), trust_env=False) as client:
        if repo:
            owner, name = repo
            payload = await _get(client, f"repos/{quote(owner)}/{quote(name)}")
            if not isinstance(payload, dict):
                return []
            result = _repo_result(payload)
            if result is None:
                return []
            readme, releases = await asyncio.gather(
                _get(client, f"repos/{quote(owner)}/{quote(name)}/readme"),
                _get(client, f"repos/{quote(owner)}/{quote(name)}/releases", {"per_page": 3}),
            )
            results = [result]
            readme_text = _decode_readme(readme)
            if readme_text:
                results.append(_source(
                    title=f"{owner}/{name} README",
                    url=f"https://github.com/{owner}/{name}#readme",
                    summary=readme_text,
                    kind="readme",
                ))
            if isinstance(releases, list):
                for item in releases[:3]:
                    if not isinstance(item, dict):
                        continue
                    release_url = str(item.get("html_url") or "").strip()
                    release_name = str(item.get("name") or item.get("tag_name") or "").strip()
                    if release_url and release_name:
                        results.append(_source(
                            title=f"{owner}/{name} Release：{release_name}",
                            url=release_url,
                            summary=str(item.get("body") or "")[:1000],
                            kind="release",
                            updated_at=str(item.get("published_at") or item.get("created_at") or ""),
                        ))
            return results[:cap]

        payload = await _get(client, "search/repositories", {"q": text[:200], "per_page": cap, "sort": "stars", "order": "desc"})
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            return []
        results = []
        for item in payload["items"]:
            if isinstance(item, dict):
                result = _repo_result(item)
                if result:
                    results.append(result)
            if len(results) >= cap:
                break
        return results


async def search_issues(query: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    """按公开 Issue/PR 搜索；只在用户明确询问时调用。"""
    text = (query or "").strip()
    if not text:
        return []
    cap = _limit(limit)
    async with httpx.AsyncClient(timeout=_timeout(), headers=_headers(), trust_env=False) as client:
        payload = await _get(client, "search/issues", {"q": text[:200], "per_page": cap, "sort": "updated", "order": "desc"})
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return []
    results = []
    for item in payload["items"]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("html_url") or "").strip()
        title = str(item.get("title") or "").strip()
        if url and title:
            results.append(_source(
                title=f"GitHub Issue/PR：{title}",
                url=url,
                summary=str(item.get("body") or "")[:1000],
                kind="issue" if "pull_request" not in item else "pull_request",
                updated_at=str(item.get("updated_at") or ""),
            ))
    return results[:cap]
