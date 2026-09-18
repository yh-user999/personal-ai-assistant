import asyncio
import logging
from types import SimpleNamespace

import pytest

from app.chat.context import ChatContext, ChatRequest
from app.chat.retrieval import retrieve
from app.chat.response_plan import build_rule_plan
from app.chat import web_provider
from app.group.web_search import GroupWebSearchLimiter, limiter


class _Memory:
    async def search(self, *args, **kwargs):
        return []

    @staticmethod
    def format_injection(items):
        return ""

    @staticmethod
    def get_recent_history(*args, **kwargs):
        return []


def _settings(**overrides):
    values = {
        "group_web_search_enabled": False,
        "group_web_search_hourly_limit": 3,
        "group_web_search_cooldown_seconds": 15.0,
        "group_web_search_budget_seconds": 8.0,
        "group_web_search_max_results": 5,
        "group_web_search_max_attempts": 2,
        "history_limit": 8,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _ctx(message: str, *, directed: bool = True) -> ChatContext:
    return ChatContext(
        request=type("R", (), {"state": type("S", (), {})()})(),
        request_model=ChatRequest(
            message=message,
            user_id="member",
            group_id="123",
            group_directed=directed,
        ),
        message=message,
        uid="member",
        is_owner=False,
        group_id="123",
        group_directed=directed,
    )


def _runtime(settings):
    return SimpleNamespace(
        settings=settings,
        memory=_Memory(),
        knowledge=object(),
        services=object(),
        logger=logging.getLogger("test.group_web_search"),
        bg_tasks=set(),
    )


@pytest.fixture(autouse=True)
def reset_limiter():
    limiter.reset()
    yield
    limiter.reset()


def test_reference_lookup_rule_is_deterministic_for_private_and_group_plans():
    plan = build_rule_plan("帮我搜一下《没钱修什么仙》的简介、作者和设定")
    assert plan.provider == "web_search"
    assert plan.intent == "external_reference_lookup"
    assert plan.investigation_required is False

    group_plan = build_rule_plan("书名是：《没钱修什么仙》", is_owner=False)
    assert group_plan.provider == "web_search"


def test_reference_lookup_stays_conservative_without_a_title():
    assert web_provider.looks_like_external_reference_lookup("这本书怎么样") is False
    assert web_provider.looks_like_external_reference_lookup("《没钱修什么仙》怎么样") is True
    assert web_provider.looks_like_external_reference_lookup("书名是：没钱修什么仙") is True


def test_reference_queries_start_with_exact_title_then_expand():
    primary, alternate = web_provider.reference_search_queries("《没钱修什么仙》这本小说的剧情")
    assert primary == "没钱修什么仙？"
    assert alternate == "没钱修什么仙？ 小说 作者 简介 剧情 设定"

    primary, alternate = web_provider.reference_search_queries("书名是：没钱修什么仙的简介")
    assert primary == "没钱修什么仙？"
    assert alternate == "没钱修什么仙？ 小说 作者 简介 剧情 设定"


def test_reference_results_drop_safe_but_irrelevant_noise():
    results = [
        {
            "title": "泛修仙流派观察",
            "url": "https://example.com/noise",
            "summary": "修仙题材趋势与网文市场",
        },
        {
            "title": "没钱修什么仙简介",
            "url": "https://example.com/book",
            "summary": "公开资料摘要",
        },
    ]
    filtered = web_provider.filter_reference_results("没钱修什么仙", results)
    assert [item["url"] for item in filtered] == ["https://example.com/book"]


def test_limiter_release_does_not_consume_hourly_quota():
    limiter_instance = GroupWebSearchLimiter()
    first = limiter_instance.reserve("123", hourly_limit=1, cooldown_seconds=0, now=10)
    assert first.allowed and first.reservation
    first.reservation.release()

    second = limiter_instance.reserve("123", hourly_limit=1, cooldown_seconds=0, now=11)
    assert second.allowed and second.reservation
    second.reservation.commit(now=11)
    blocked = limiter_instance.reserve("123", hourly_limit=1, cooldown_seconds=0, now=12)
    assert blocked.allowed is False
    assert blocked.reason == "hourly_limit"


def test_limiter_enforces_cooldown_then_expires():
    limiter_instance = GroupWebSearchLimiter()
    first = limiter_instance.reserve("123", hourly_limit=3, cooldown_seconds=15, now=10)
    assert first.allowed and first.reservation
    first.reservation.commit(now=10)

    blocked = limiter_instance.reserve("123", hourly_limit=3, cooldown_seconds=15, now=20)
    assert blocked.allowed is False
    assert blocked.reason == "cooldown"

    second = limiter_instance.reserve("123", hourly_limit=3, cooldown_seconds=15, now=25)
    assert second.allowed and second.reservation
    second.reservation.release()


def test_limiter_isolates_groups_and_bounds_pending_capacity():
    limiter_instance = GroupWebSearchLimiter(max_groups=1)
    first = limiter_instance.reserve("123", hourly_limit=3, cooldown_seconds=0, now=10)
    assert first.allowed and first.reservation
    other = limiter_instance.reserve("456", hourly_limit=3, cooldown_seconds=0, now=10)
    assert other.allowed is False
    assert other.reason == "capacity"
    first.reservation.release()

    isolated = limiter_instance.reserve("456", hourly_limit=3, cooldown_seconds=0, now=11)
    assert isolated.allowed and isolated.reservation
    isolated.reservation.commit(now=11)
    assert limiter_instance.snapshot("123", now=11)["started"] == 0
    assert limiter_instance.snapshot("456", now=11)["started"] == 1


def test_group_web_search_is_disabled_by_default(monkeypatch):
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("默认关闭时不应联网")

    monkeypatch.setattr(web_provider, "search_and_cluster", forbidden)
    ctx = _ctx("《没钱修什么仙》简介")
    ctx.trace.response_plan = {"provider": "web_search"}
    bundle = asyncio.run(retrieve(ctx, _runtime(_settings()), None))

    assert calls == []
    assert bundle.evidence == {}
    assert bundle.trace["group_web_search"] == "disabled"


def test_group_web_search_requires_directed_message(monkeypatch):
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("非直达消息不应联网")

    monkeypatch.setattr(web_provider, "configured", lambda: True)
    monkeypatch.setattr(web_provider, "search_and_cluster", forbidden)
    ctx = _ctx("《没钱修什么仙》简介", directed=False)
    ctx.trace.response_plan = {"provider": "web_search"}
    bundle = asyncio.run(
        retrieve(ctx, _runtime(_settings(group_web_search_enabled=True)), None)
    )

    assert calls == []
    assert bundle.trace["group_web_search"] == "not_directed"


def test_group_web_search_injects_only_current_sources(monkeypatch):
    async def fake_search(*args, **kwargs):
        return {
            "results": [
                {
                    "title": "没钱修什么仙简介",
                    "url": "https://example.com/book",
                    "source": "example",
                    "published_at": "2026-09-18T00:00:00Z",
                    "summary": "公开检索摘要",
                }
            ],
            "events": [],
            "observed_at": "2026-09-18T00:00:00Z",
        }

    monkeypatch.setattr(web_provider, "configured", lambda: True)
    monkeypatch.setattr(web_provider, "search_and_cluster", fake_search)
    ctx = _ctx("《没钱修什么仙》简介")
    ctx.trace.response_plan = {"provider": "web_search"}
    bundle = asyncio.run(
        retrieve(ctx, _runtime(_settings(group_web_search_enabled=True)), None)
    )

    assert bundle.evidence["sources"][0]["url"] == "https://example.com/book"
    assert "公开检索摘要" in bundle.knowledge_text
    assert bundle.trace["group_web_search"] == "ok"
    assert bundle.trace["retrieval"]["web_sources"] == 1


def test_group_web_search_treats_unrelated_results_as_no_sources(monkeypatch):
    async def noisy_search(*args, **kwargs):
        return {
            "results": [
                {
                    "title": "2026 网文流派观察",
                    "url": "https://example.com/noise",
                    "source": "example",
                    "published_at": "2026-09-18T00:00:00Z",
                    "summary": "泛修仙题材与市场趋势",
                }
            ],
            "events": [],
            "observed_at": "2026-09-18T00:00:00Z",
        }

    monkeypatch.setattr(web_provider, "configured", lambda: True)
    monkeypatch.setattr(web_provider, "search_and_cluster", noisy_search)
    ctx = _ctx("《没钱修什么仙》简介")
    ctx.trace.response_plan = {"provider": "web_search"}
    bundle = asyncio.run(
        retrieve(ctx, _runtime(_settings(group_web_search_enabled=True)), None)
    )

    assert bundle.evidence["sources"] == []
    assert bundle.knowledge_text == ""
    assert bundle.trace["group_web_search"] == "no_sources"


def test_group_web_search_hourly_limit_is_enforced(monkeypatch):
    async def fake_search(*args, **kwargs):
        return {
            "results": [
                {
                    "title": "没钱修什么仙公开资料",
                    "url": "https://example.com/book",
                    "source": "example",
                    "published_at": "2026-09-18T00:00:00Z",
                    "summary": "没钱修什么仙摘要",
                }
            ],
            "events": [],
            "observed_at": "2026-09-18T00:00:00Z",
        }

    monkeypatch.setattr(web_provider, "configured", lambda: True)
    monkeypatch.setattr(web_provider, "search_and_cluster", fake_search)
    settings = _settings(group_web_search_enabled=True, group_web_search_hourly_limit=1, group_web_search_cooldown_seconds=0)

    first = _ctx("《没钱修什么仙》简介")
    first.trace.response_plan = {"provider": "web_search"}
    assert asyncio.run(retrieve(first, _runtime(settings), None)).trace["group_web_search"] == "ok"

    second = _ctx("《没钱修什么仙》作者")
    second.trace.response_plan = {"provider": "web_search"}
    bundle = asyncio.run(retrieve(second, _runtime(settings), None))
    assert bundle.trace["group_web_search"] == "hourly_limit"


def test_group_web_search_releases_reservation_when_no_sources(monkeypatch):
    async def empty_search(*args, **kwargs):
        return {"results": [], "events": [], "observed_at": "2026-09-18T00:00:00Z"}

    monkeypatch.setattr(web_provider, "configured", lambda: True)
    monkeypatch.setattr(web_provider, "search_and_cluster", empty_search)
    settings = _settings(
        group_web_search_enabled=True,
        group_web_search_hourly_limit=1,
        group_web_search_cooldown_seconds=15,
    )

    first = _ctx("《没钱修什么仙》简介")
    first.trace.response_plan = {"provider": "web_search"}
    assert asyncio.run(retrieve(first, _runtime(settings), None)).trace["group_web_search"] == "no_sources"

    second = _ctx("《没钱修什么仙》作者")
    second.trace.response_plan = {"provider": "web_search"}
    assert asyncio.run(retrieve(second, _runtime(settings), None)).trace["group_web_search"] == "no_sources"
