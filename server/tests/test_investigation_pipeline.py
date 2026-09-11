"""主动调查的接线回归：证据共用、旧摘要仅线索、失败不放行、关闭与取消边界。"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.chat import investigation, pipeline, prompting, retrieval, review
from app.config import settings
from tests.test_chat_retrieval import (
    db_env as db_env,  # noqa: F401
    _patch_memory_scoped,
    _sources,
    _web_retrieval_env,
    make_ctx,
)


def _evidence(question="事件甲"):
    text = "事件甲的材料只记录调解，未说明后续动作。"
    return {
        "version": 1, "question": question, "status": "complete", "stop_reason": "round_limit",
        "sources": [{"id": "s1", "url": "https://news.example.com/a", "title": "事件甲记录",
                     "text": text, "origin": "记录方", "kind": "webpage", "published_at": ""}],
        "claims": [{"id": "c1", "event": "事件甲", "actor": "双方", "action": "调解",
                    "target": "纠纷", "occurred_at": "", "statement": text, "status": "unknown",
                    "attribution": "记录方陈述", "support": [{"source_id": "s1", "quote": text}], "oppose": []}],
        "gaps": [{"id": "actions", "question": "后续各自采取什么动作？", "why": "影响判断", "query": "事件甲 完整经过", "priority": 90}],
        "checks": {"mode": "gap_driven_investigation", "dimensions": {}},
    }


def test_evidence_shared_by_generation_review_and_trace_summary(db_env, monkeypatch):
    _, runtime = _web_retrieval_env(monkeypatch, results=_sources())
    _patch_memory_scoped(monkeypatch)
    evidence = _evidence()
    summary = {"version": 1, "question": "事件甲", "open_questions": ["后续各自采取什么动作？"], "searched_queries": ["事件甲 完整经过"]}
    calls = []

    async def fake_investigate(query, initial_results, runtime, **kwargs):
        calls.append((query, kwargs))
        return investigation.InvestigationResult(initial_results, evidence, summary, {
            "rounds": 1, "search_calls": 2, "pages_read": 1, "claim_count": 1, "gap_count": 1,
            "elapsed_ms": 15, "stop_reason": "round_limit",
        })

    monkeypatch.setattr(investigation, "investigate", fake_investigate)
    ctx = make_ctx("现在呢")
    ctx.trace.response_plan = {"provider": "web_search", "query": "事件甲", "investigation_required": True}
    ctx.trace.investigation_context = summary
    bundle = asyncio.run(retrieval.retrieve(ctx, runtime, retrieval.prepare_turn(ctx, runtime)))
    assert calls[0][0] == "事件甲", "不能拿指代式追问当主检索词"
    assert calls[0][1]["previous"] == summary
    assert bundle.evidence is evidence
    system = prompting.build_system_prompt(ctx, runtime, bundle)
    assert "后续各自采取什么动作" in system
    assert "不等于该说法已被证实" in system
    reviewed = json.loads(review.build_review_messages(ctx, bundle, "草稿")[1]["content"])
    assert reviewed["evidence"]["sources"][0]["text"] == evidence["sources"][0]["text"]
    assert reviewed["evidence"]["gaps"][0]["id"] == "actions"
    assert ctx.trace.response_plan["investigation_search_calls"] == 2
    assert ctx.trace.response_plan["investigation_resumed"]
    assert "claim_analysis" not in ctx.trace.stages, "不能在新调查后又跑一次旧LLM抽取"


def test_disabled_investigation_does_not_run_controller(db_env, monkeypatch):
    calls, runtime = _web_retrieval_env(monkeypatch, results=_sources())
    _patch_memory_scoped(monkeypatch)
    monkeypatch.setattr(settings, "investigation_enabled", False)

    async def forbidden(*args, **kwargs):
        raise AssertionError("关闭开关仍调用调查器")

    monkeypatch.setattr(investigation, "investigate", forbidden)
    ctx = make_ctx("事件甲怎么看")
    ctx.trace.response_plan = {"provider": "web_search", "investigation_required": True}
    bundle = asyncio.run(retrieval.retrieve(ctx, runtime, retrieval.prepare_turn(ctx, runtime)))
    assert "investigation" not in ctx.trace.stages
    assert calls["search"] == 1, "关闭调查后不能退回固定角度深挖"
    assert bundle.evidence.get("checks", {}).get("mode") != "gap_driven_investigation"


def test_investigation_failure_keeps_sources_but_not_verified_status(db_env, monkeypatch):
    _, runtime = _web_retrieval_env(monkeypatch, results=_sources())
    _patch_memory_scoped(monkeypatch)

    async def failed(*args, **kwargs):
        raise RuntimeError("simulated analysis failure")

    monkeypatch.setattr(investigation, "investigate", failed)
    ctx = make_ctx("事件甲怎么看")
    ctx.trace.response_plan = {"provider": "web_search", "investigation_required": True}
    bundle = asyncio.run(retrieval.retrieve(ctx, runtime, retrieval.prepare_turn(ctx, runtime)))
    assert bundle.evidence["sources"]
    assert bundle.evidence["status"] == "failed"
    assert review.investigation_safety_fallback(bundle)
    assert ctx.trace.response_plan["web_has_sources"]
    assert ctx.trace.response_plan["investigation_status"] == "failed"


def test_external_cancellation_is_not_swallowed(db_env, monkeypatch):
    _, runtime = _web_retrieval_env(monkeypatch, results=_sources())
    _patch_memory_scoped(monkeypatch)

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(investigation, "investigate", cancelled)
    ctx = make_ctx("事件甲怎么看")
    ctx.trace.response_plan = {"provider": "web_search", "investigation_required": True}
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(retrieval.retrieve(ctx, runtime, retrieval.prepare_turn(ctx, runtime)))


def test_review_timeout_is_not_recorded_as_passed(db_env, monkeypatch):
    ctx = make_ctx("事件甲谁的错")
    ctx.trace.response_plan = {"investigation_status": "complete", "needs_moral_judgment": True}
    evidence = _evidence()
    bundle = SimpleNamespace(evidence=evidence, facts="", lessons="")
    checked = review.ReplyReview(status="timeout")

    async def timed_out(*args, **kwargs):
        return checked, 10

    monkeypatch.setattr(review, "review_reply", timed_out)
    monkeypatch.setattr(review, "persist_review", lambda *args, **kwargs: None)
    runtime = SimpleNamespace(settings=settings)
    answer = asyncio.run(pipeline._reflect_and_finalize_reply(ctx, runtime, bundle, "未经核实的肯定指控"))
    assert answer != "未经核实的肯定指控"
    assert ctx.trace.reflection["status"] == "timeout"
    assert ctx.trace.reflection["safety_fallback"]


def test_failed_investigation_cannot_be_overruled_by_passed_review(db_env, monkeypatch):
    ctx = make_ctx("事件甲谁的错")
    ctx.trace.response_plan = {"investigation_status": "failed", "needs_moral_judgment": True}
    evidence = _evidence()
    evidence["status"] = "failed"
    evidence["claims"] = []
    bundle = SimpleNamespace(evidence=evidence, facts="", lessons="")

    async def passed(*args, **kwargs):
        return review.ReplyReview(status="passed", scores={"safety": 1, "relevance": 1}), 2

    monkeypatch.setattr(review, "review_reply", passed)
    monkeypatch.setattr(review, "persist_review", lambda *args, **kwargs: None)
    answer = asyncio.run(pipeline._reflect_and_finalize_reply(ctx, SimpleNamespace(settings=settings), bundle, "两次抽取均失败却给出肯定指控"))
    assert "肯定指控" not in answer
    assert ctx.trace.reflection["status"] == "investigation_incomplete"
    assert ctx.trace.reflection["safety_fallback"]


def test_real_initial_search_and_followup_share_total_budget(db_env, monkeypatch):
    from app.chat import web_provider
    import time

    real_search = web_provider.search_and_cluster
    _, runtime = _web_retrieval_env(monkeypatch)
    _patch_memory_scoped(monkeypatch)
    monkeypatch.setattr(web_provider, "search_and_cluster", real_search)
    monkeypatch.setattr(settings, "investigation_budget", 0.05)
    monkeypatch.setattr(settings, "gdelt_enabled", False)
    cancelled = []

    async def slow(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(web_provider, "web_search", slow)
    ctx = make_ctx("事件甲怎么看")
    ctx.trace.response_plan = {"provider": "web_search", "investigation_required": True}
    started = time.monotonic()
    bundle = asyncio.run(retrieval.retrieve(ctx, runtime, retrieval.prepare_turn(ctx, runtime)))
    assert time.monotonic() - started < 1.0
    assert cancelled, "必须取消真正的初检子任务"
    assert ctx.trace.response_plan["investigation_elapsed_ms"] < 300
    assert bundle.evidence["status"] != "complete"


def test_plain_news_evidence_is_not_an_investigation():
    evidence = investigation.build_evidence("今天的新闻", _sources())
    assert evidence["sources"]
    assert not evidence.get("claims")
    assert review.investigation_safety_fallback(SimpleNamespace(evidence=evidence)) is None
