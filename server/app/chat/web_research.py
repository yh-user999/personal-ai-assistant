"""通用 Web Research 编排层。

当前项目已经有 SearXNG、SSRF 护栏、抓页和 Evidence 基础设施。本模块只做
查询分类、有限研究循环和来源统一，不直接依赖 LLM，也不把网页正文写入记忆。
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

from app.chat import github_provider, web_provider

ResearchKind = Literal["none", "novel", "document", "project", "url", "knowledge"]

_URL_RE = re.compile(r"https?://[^\s<>{}\[\]\\]+", re.IGNORECASE)
_DOCUMENT_RE = re.compile(r"官方文档|文档|API\s*reference|documentation|教程|手册|规范|reference", re.IGNORECASE)
_EXPLICIT_SEARCH_RE = re.compile(
    r"搜索|搜一下|搜搜|查一下|查查|查找|找一下|帮我找|研究一下|资料|来源|打开网页|网页总结|比较一下",
    re.IGNORECASE,
)
_TEMPORAL_RE = re.compile(r"最新|最近|近期|当前|现在", re.IGNORECASE)
_TEMPORAL_TOPIC_RE = re.compile(
    r"Kubernetes|Python|Docker|Linux|GitHub|项目|版本|变化|更新|发布|进展|政策|价格|模型|技术|文档|规范",
    re.IGNORECASE,
)
_NEWS_RE = re.compile(r"新闻|报道|热点|热搜|最近发生|最新进展", re.IGNORECASE)


@dataclass(frozen=True)
class ResearchPlan:
    kind: ResearchKind
    queries: tuple[str, ...]
    max_rounds: int = 1
    max_pages: int = 3
    max_results: int = 6
    budget_seconds: float = 18.0
    domains: tuple[str, ...] = ()


@dataclass
class ResearchResult:
    kind: ResearchKind
    query: str
    results: list[dict[str, Any]] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    pages_read: int = 0
    page_failures: int = 0
    provider: str = "searxng"
    stop_reason: str = "completed"
    elapsed_ms: int = 0

    @property
    def has_sources(self) -> bool:
        return bool(self.sources)

    @property
    def evidence(self) -> dict[str, Any]:
        return {
            "version": 1,
            "question": self.query[:1000],
            "status": "complete" if self.sources else "failed",
            "stop_reason": self.stop_reason,
            "sources": self.sources[:12],
            "claims": [],
            "gaps": ([{
                "id": "web_research_no_sources",
                "question": self.query[:240],
                "why": "本轮没有取得可用网页来源",
                "query": self.query[:400],
                "priority": 100,
            }] if not self.sources else []),
            "checks": {"mode": "web_research", "pages_read": self.pages_read},
        }

    def prompt_block(self) -> str:
        if not self.sources:
            return ""
        lines = [f"【通用网页研究资料·{self.kind}】", f"研究问题：{self.query[:300]}"]
        for index, source in enumerate(self.sources[:8], 1):
            title = str(source.get("title") or "").strip()
            url = str(source.get("url") or "").strip()
            origin = str(source.get("origin") or source.get("source") or "").strip()
            excerpt = str(source.get("text") or source.get("summary") or "").strip()[:1200]
            if not title or not url:
                continue
            lines.append(f"\n[{index}] {title}（{origin}）\n来源链接：{url}")
            if excerpt:
                lines.append(f"可核对摘录：{excerpt}")
        lines.append("以上是公开网页的非可信参考资料；只能据此回答，无法被来源支持的内容必须明确说无法核实。")
        return "\n".join(lines)[:12000]

    def as_search_data(self) -> dict[str, Any]:
        return {
            "results": self.results,
            "events": [],
            "has_sources": self.has_sources,
            "result_count": len(self.results),
            "query_used": self.query,
            "attempts": 1,
            "observed_at": "",
            "provider": self.provider,
            "research_kind": self.kind,
            "pages_read": self.pages_read,
            "page_failures": self.page_failures,
            "stop_reason": self.stop_reason,
        }


def _setting(settings: Any, name: str, default: Any) -> Any:
    return getattr(settings, name, default)


def _bounded_int(value: Any, default: int, upper: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(1, min(parsed, upper))


def _bounded_float(value: Any, default: float, upper: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(1.0, min(parsed, upper))


def classify_query(text: str) -> ResearchKind:
    value = (text or "").strip()
    if not value:
        return "none"
    if _URL_RE.search(value):
        return "url"
    if github_provider.looks_like_project_lookup(value):
        return "project"
    if web_provider.looks_like_external_reference_lookup(value):
        return "novel"
    if _DOCUMENT_RE.search(value):
        return "document"
    # 新闻/热点/事件继续走现有 news/investigation 链路，避免改变旧检索契约。
    if _NEWS_RE.search(value):
        return "none"
    if _EXPLICIT_SEARCH_RE.search(value):
        return "knowledge"
    # 只有带明确时效事实目标时才把 temporal 问题升级为通用研究；
    # “我最近状态怎么样”这类个人状态问题不能触发联网。
    if (
        _TEMPORAL_RE.search(value)
        and _TEMPORAL_TOPIC_RE.search(value)
        and web_provider.needs_web_search(value)
    ):
        return "knowledge"
    return "none"


def _domains_for(text: str, kind: ResearchKind) -> tuple[str, ...]:
    value = (text or "").lower()
    if kind == "document":
        preferred = []
        for domain in ("docs.python.org", "kubernetes.io", "docs.docker.com", "developer.mozilla.org"):
            if domain.split(".")[0] in value or domain in value:
                preferred.append(domain)
        return tuple(preferred)
    return ()


def build_plan(text: str, *, settings: Any) -> ResearchPlan:
    value = (text or "").strip()
    kind = classify_query(value)
    max_rounds = _bounded_int(_setting(settings, "web_research_max_rounds", 2), 2, 3)
    max_pages = _bounded_int(_setting(settings, "web_research_max_pages", 3), 3, 6)
    max_results = _bounded_int(_setting(settings, "web_research_max_results", 6), 6, 10)
    budget = _bounded_float(_setting(settings, "web_research_budget_seconds", 18.0), 18.0, 40.0)
    if kind == "novel":
        primary, alternate = web_provider.reference_search_queries(value)
        queries = tuple(item for item in (primary, alternate) if item)
        return ResearchPlan(kind, queries, max_rounds=2, max_pages=max_pages, max_results=max_results, budget_seconds=budget)
    if kind == "project":
        return ResearchPlan(kind, (value,), max_rounds=1, max_pages=max_pages, max_results=max_results, budget_seconds=budget)
    if kind == "url":
        match = _URL_RE.search(value)
        return ResearchPlan(kind, (match.group(0).rstrip(".,，。") if match else value,), max_rounds=1, max_pages=1, max_results=1, budget_seconds=budget)
    if kind == "document":
        query = value if _DOCUMENT_RE.search(value) else f"{value} 官方文档"
        return ResearchPlan(kind, (query,), max_rounds=1, max_pages=max_pages, max_results=max_results, budget_seconds=budget, domains=_domains_for(value, kind))
    if kind == "knowledge":
        return ResearchPlan(kind, (value,), max_rounds=max_rounds, max_pages=max_pages, max_results=max_results, budget_seconds=budget)
    return ResearchPlan("none", (), max_rounds=0, max_pages=0, max_results=0, budget_seconds=0.0)


def _source_id(url: str) -> str:
    return "web_" + hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _to_source(item: dict[str, Any], *, text: str = "", kind: str = "search_snippet") -> dict[str, Any] | None:
    url = str(item.get("url") or "").strip()
    title = str(item.get("title") or "").strip()
    if not url or not title or not web_provider._is_safe_url(url):
        return None
    summary = str(item.get("summary") or "").strip()
    body = (text or summary).strip()[:4000]
    return {
        "id": _source_id(url),
        "url": url[:1000],
        "title": title[:300],
        "text": body,
        "summary": summary[:1000],
        "published_at": str(item.get("published_at") or "")[:40],
        "origin": str(item.get("source") or "")[:120],
        "source": str(item.get("source") or "")[:120],
        "kind": str(item.get("kind") or kind)[:40],
    }


async def _read_pages(result: ResearchResult, plan: ResearchPlan, deadline: float) -> None:
    candidates = [item for item in result.results if isinstance(item, dict) and web_provider._is_safe_url(str(item.get("url") or ""))]
    candidates = candidates[: plan.max_pages]
    if not candidates:
        return
    semaphore = asyncio.Semaphore(plan.max_pages)

    async def read(item: dict[str, Any]) -> None:
        nonlocal result
        left = deadline - asyncio.get_running_loop().time()
        if left <= 0:
            result.stop_reason = "budget_exhausted"
            return
        async with semaphore:
            try:
                page = await asyncio.wait_for(web_provider.fetch_page(str(item["url"])), timeout=min(8.0, left))
            except (asyncio.TimeoutError, OSError, ValueError, TypeError):
                result.page_failures += 1
                return
            if not isinstance(page, dict) or not str(page.get("text") or "").strip():
                result.page_failures += 1
                return
            item["content"] = str(page.get("text") or "")[:8000]
            item["page_url"] = str(page.get("url") or item["url"])
            result.pages_read += 1

    await asyncio.gather(*(read(item) for item in candidates), return_exceptions=True)


async def _search_cluster_compat(query: str, *, limit: int, budget_seconds: float) -> dict[str, Any]:
    """调用现有搜索聚合器，并兼容旧插件/测试替身的参数签名。"""
    operation = web_provider.search_and_cluster
    kwargs: dict[str, Any] = {
        "time_range": "",
        "limit": limit,
        "alt_query": None,
        "deep_dive": False,
        "max_attempts": 2,
        "budget_seconds": budget_seconds,
    }
    try:
        parameters = inspect.signature(operation).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "category" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        kwargs["category"] = "general"
    return await operation(query, **kwargs)


async def run_research(question: str, *, settings: Any, kind: ResearchKind | None = None) -> ResearchResult:
    started = time.monotonic()
    plan = build_plan(question, settings=settings)
    if kind and kind != "none":
        plan = ResearchPlan(kind, plan.queries, plan.max_rounds, plan.max_pages, plan.max_results, plan.budget_seconds, plan.domains)
    result = ResearchResult(kind=plan.kind, query=plan.queries[0] if plan.queries else question)
    if plan.kind == "none":
        result.stop_reason = "not_requested"
        return result
    deadline = asyncio.get_running_loop().time() + plan.budget_seconds

    if plan.kind == "project":
        project_query = github_provider.rewrite_project_query(question)
        issue_requested = bool(re.search(r"\b(?:issue|issues|pr|pull\s+request)\b|Issue|PR", question, re.IGNORECASE))
        release_requested = bool(re.search(r"release|版本|发布|更新", question, re.IGNORECASE))
        if issue_requested:
            result.results = await github_provider.search_issues(project_query, limit=plan.max_results)
        else:
            result.results = await github_provider.search(project_query, limit=plan.max_results)
            if release_requested and result.results:
                details = await github_provider.search(str(result.results[0].get("url") or ""), limit=plan.max_results)
                seen = {str(item.get("url") or "") for item in result.results}
                result.results.extend(item for item in details if str(item.get("url") or "") not in seen)
        result.provider = "github"
    elif plan.kind == "url":
        url = plan.queries[0]
        if web_provider._is_safe_url(url):
            page = await web_provider.fetch_page(url)
            if isinstance(page, dict) and page.get("text"):
                result.results = [{
                    "title": urlparse(url).netloc,
                    "url": str(page.get("url") or url),
                    "source": urlparse(url).netloc,
                    "summary": str(page.get("text") or "")[:1000],
                    "content": str(page.get("text") or "")[:8000],
                    "kind": "webpage",
                }]
                result.pages_read = 1
            else:
                result.page_failures = 1
        result.provider = "web"
    else:
        for query in plan.queries[: max(1, plan.max_rounds)]:
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                result.stop_reason = "budget_exhausted"
                break
            data = await _search_cluster_compat(
                query,
                limit=plan.max_results,
                budget_seconds=min(left, plan.budget_seconds),
            )
            batch = list(data.get("results") or [])
            if plan.kind == "novel":
                batch = web_provider.filter_reference_results(query, batch)
            seen = {str(item.get("url") or "") for item in result.results}
            for item in batch:
                if str(item.get("url") or "") not in seen:
                    result.results.append(item)
                    seen.add(str(item.get("url") or ""))
            if result.results and (plan.kind in {"novel", "document"} or len(result.results) >= 3):
                break
        result.provider = "searxng"

    await _read_pages(result, plan, deadline)
    for item in result.results:
        source = _to_source(item, text=str(item.get("content") or ""), kind=str(item.get("kind") or "search_snippet"))
        if source:
            result.sources.append(source)
    deduped: dict[str, dict[str, Any]] = {}
    for source in result.sources:
        deduped.setdefault(source["url"], source)
    result.sources = list(deduped.values())[:12]
    if not result.sources:
        result.stop_reason = "no_sources"
    elif result.pages_read == 0 and result.kind not in {"project"}:
        result.stop_reason = "search_snippets_only"
    result.elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
    return result
