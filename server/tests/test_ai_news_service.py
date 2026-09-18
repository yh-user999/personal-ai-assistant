"""每日 AI 资讯日报服务、来源约束与幂等回归。"""
from __future__ import annotations

import asyncio

from app.ai_news import service
from app.ai_news.repository import repository
from app.config import settings


def _search_payload(index: int) -> dict:
    return {
        "request_count": 1,
        "results": [
            {
                "title": f"AI 更新 {index}",
                "url": f"https://example.com/ai/{index}",
                "source": "example.com",
                "published_at": "2026-09-18T07:00:00+08:00",
                "time_known": True,
                "summary": "公开来源摘要",
            }
        ],
    }


def test_source_rows_deduplicate_urls_and_titles():
    rows = service._source_rows(
        [
            {"results": [_search_payload(1)["results"][0]]},
            {"results": [_search_payload(1)["results"][0], _search_payload(2)["results"][0]]},
        ],
        10,
    )
    assert [row["url"] for row in rows] == [
        "https://example.com/ai/1",
        "https://example.com/ai/2",
    ]


def test_source_rows_reject_non_http_urls():
    rows = service._source_rows(
        [{"results": [{"title": "异常来源", "url": "javascript:alert(1)", "source": "invalid"}]}],
        10,
    )
    assert rows == []


def test_digest_is_idempotent_and_preserves_source(monkeypatch, db):
    search_calls: list[str] = []
    llm_calls: list[str] = []

    async def fake_search(query, **kwargs):
        search_calls.append(query)
        return _search_payload(len(search_calls))

    async def fake_chat_json(*args, **kwargs):
        llm_calls.append("called")
        return {
            "title": "AI 资讯日报",
            "items": [
                {
                    "category": "model",
                    "headline": "今日模型更新",
                    "facts": ["来源确认有一项更新"],
                    "why_it_matters": "便于跟踪模型能力变化",
                    "confidence": "high",
                    "sources": [{"url": "https://example.com/ai/1"}],
                }
            ],
            "editor_note": "仅依据已列来源。",
        }

    monkeypatch.setattr(settings, "ai_news_digest_enabled", True)
    monkeypatch.setattr(settings, "ai_news_digest_max_items", 8)
    monkeypatch.setattr(service.web_provider, "search_and_cluster", fake_search)
    monkeypatch.setattr(service.llm, "chat_json", fake_chat_json)

    first = asyncio.run(service.run_daily_ai_news_digest(user_id="owner"))
    second = asyncio.run(service.run_daily_ai_news_digest(user_id="owner"))

    assert first["status"] == "ready"
    assert first["push_status"] == "not_configured"
    assert first["stats"]["item_count"] == 1
    assert second["skipped"] is True
    assert len(search_calls) == 4
    assert len(llm_calls) == 1
    stored = repository.get("owner")
    assert stored and stored["status"] == "ready"
    assert stored["sources"][0]["url"] == "https://example.com/ai/1"


def test_digest_failure_is_recorded_without_fake_content(monkeypatch, db):
    async def no_results(*args, **kwargs):
        return {"request_count": 1, "results": []}

    monkeypatch.setattr(settings, "ai_news_digest_enabled", True)
    monkeypatch.setattr(service.web_provider, "search_and_cluster", no_results)

    result = asyncio.run(service.run_daily_ai_news_digest(user_id="owner"))

    assert result["status"] == "failed"
    digest_date = service.datetime.now(service.TZ).date().isoformat()
    stored = repository.get("owner", digest_date)
    assert stored and stored["status"] == "failed"
    assert stored["content"] == ""
    assert stored["error"] == "ValueError"


def test_digest_push_failure_does_not_lose_saved_digest(monkeypatch, db):
    async def fake_search(query, **kwargs):
        return _search_payload(1)

    async def fake_chat_json(*args, **kwargs):
        return {
            "items": [
                {
                    "headline": "一条资讯",
                    "facts": ["事实"],
                    "sources": [{"url": "https://example.com/ai/1"}],
                }
            ]
        }

    async def failed_push(text):
        return False

    monkeypatch.setattr(settings, "ai_news_digest_enabled", True)
    monkeypatch.setattr(settings, "qq_push_url", "https://push.example.test")
    monkeypatch.setattr(settings, "qq_admin_id", "10001")
    monkeypatch.setattr(service.web_provider, "search_and_cluster", fake_search)
    monkeypatch.setattr(service.llm, "chat_json", fake_chat_json)
    monkeypatch.setattr(service.qq_push, "send_private", failed_push)

    result = asyncio.run(service.run_daily_ai_news_digest(user_id="10001"))

    assert result["status"] == "ready"
    assert result["push_status"] == "failed"
    assert repository.get("10001")["content"]


def test_failed_digest_is_upserted_without_a_generation_row(db):
    repository.save_failed("10003", "2026-09-18", "RuntimeError")
    stored = repository.get("10003", "2026-09-18")
    assert stored and stored["status"] == "failed"
    assert stored["content"] == ""
    assert stored["error"] == "RuntimeError"


def test_repository_scopes_digest_by_user(db):
    repository.begin("owner", "2026-09-18")
    repository.save_ready("owner", "2026-09-18", "主人日报", [], {})
    assert repository.get("owner", "2026-09-18")["content"] == "主人日报"
    assert repository.get("10002", "2026-09-18") is None
