"""聊天响应流水线测试：命令短路、LLM 失败友好响应、长文重试、消息持久化。"""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from app.chat.context import ChatContext, ChatRequest, ChatRuntime
from app.chat.pipeline import run_chat
from app.config import settings
from app.core import knowledge as knowledge_module
from app.core import memory as memory_module
from app.models.database import connect, init_db, reset_connections


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "api_token", "")
    reset_connections()
    init_db()
    # embedding 是单例模块，memory/knowledge 共享同一对象——一次打点全覆盖
    import app.core.embedding as _embedding

    async def fake_embed(texts):
        return [[0.0] * settings.embedding_dimension for _ in texts]

    monkeypatch.setattr(_embedding, "embed", fake_embed)
    yield
    reset_connections()


class _Services:
    """通用空值桩集合：所有 pipeline/routing/retrieve 用到的方法都安全返回。"""

    def __init__(self):
        self.confirm = SimpleNamespace(
            peek=lambda uid: None,
            parse_reply=lambda msg: None,
            take=lambda uid: None,
            clear=lambda uid: None,
            remember=lambda *a, **k: None,
            needs_confirm=lambda *a, **k: False,
        )
        self.worklog = SimpleNamespace(add_log=lambda content: None)
        self.reminders = SimpleNamespace(
            parse_reminder_cmd=lambda msg: None,
            add_reminder=lambda *a, **k: None,
            list_pending=list,
            cancel_by_keyword=lambda kw: 0,
        )
        self.documents = SimpleNamespace(parse_doc_command=lambda msg: None)
        self.resume = SimpleNamespace(parse_resume_command=lambda msg: None)
        self.goals = SimpleNamespace(
            parse_goal_command=lambda msg: None,
            add_goal=lambda *a, **k: None,
            complete_goal=lambda *a, **k: False,
            update_progress=lambda *a, **k: False,
            get_goals_injection=lambda **k: "",
        )
        self.fitness = SimpleNamespace(
            parse_weight=lambda msg: None,
            parse_training=lambda msg: None,
            get_fitness_facts=lambda msg: [],
            PROGRESS_WORDS=frozenset(),
        )
        self.novel_writing = SimpleNamespace(
            parse_writing_log=lambda msg: None,
            parse_conflict_command=lambda msg: None,
            parse_continue_command=lambda msg: None,
        )
        self.chapter_analysis = SimpleNamespace(
            parse_analysis_command=lambda msg: None,
            parse_archive_command=lambda msg: None,
            build_continuity_block=lambda: "",
            extract_chapter_no=lambda text: None,
        )
        self.message_search = SimpleNamespace(parse_search_command=lambda msg: None)
        self.executor = SimpleNamespace(parse_executor_command=lambda msg: None)
        self.cooccurrence = SimpleNamespace(expand=lambda mems, **k: mems)
        self.subjective_time = SimpleNamespace(format_injection=lambda mems: "")
        self.knowledge_domain = SimpleNamespace(
            detect_domains=lambda msg: ([], []),
            register_class=lambda *a, **k: None,
            detect_enum_intent=lambda *a, **k: False,
        )
        self.index_healer = SimpleNamespace(
            apply_correction=lambda msg: None,
            diagnose=lambda *a, **k: None,
            detect_enum_intent=lambda *a, **k: False,
            heal=None,
        )
        self.novel_entities = SimpleNamespace(build_entity_context=lambda msg: "")
        self.profile = SimpleNamespace(get_profile_injection=lambda **k: "")
        self.self_reflect = SimpleNamespace(
            detect_correction=lambda msg: False,
            classify_lesson=lambda msg: "fact",
            save_lesson=lambda *a, **k: 0,
            get_lessons_injection=lambda **k: "",
        )
        self.identity_guard = SimpleNamespace(
            is_roleplay_or_insult=lambda msg: False,
            peek=lambda uid: None,
            take=lambda uid: None,
            clear=lambda uid: None,
            remember=lambda *a, **k: None,
            check=lambda msg: (None, None),
        )
        self.fact_extract = SimpleNamespace(
            maybe_extract_facts=lambda *a, **k: _noop(),
            extract_from_last_ai=lambda *a, **k: _noop(),
            is_record_command=lambda msg: False,
            is_short_confirm=lambda msg: False,
            last_ai_looks_like_setting=lambda last_ai: False,
        )
        self.concern_tracker = SimpleNamespace(get_concerns_injection=lambda **k: "")
        self.jargon = SimpleNamespace(
            detect_definition=lambda msg: None,
            save_term=lambda *a, **k: None,
            get_jargon_injection=lambda *a, **k: "",
        )
        self.few_shot = SimpleNamespace(
            detect_positive_feedback=lambda msg: False,
            save_example=lambda *a, **k: 0,
            get_examples_injection=lambda **k: "",
        )
        self.behavior_context = SimpleNamespace(get_behavior_injection=lambda **k: "")
        self.growth = SimpleNamespace(
            detect_self_doubt=lambda msg: False,
            build_injection=lambda **k: "",
        )
        self.intent_goals = SimpleNamespace(
            record_intent=lambda *a, **k: None,
            build_injection=lambda **k: "",
        )
        self.knowledge_hint = SimpleNamespace(build_hint=lambda msg: "")
        self.unresolved = SimpleNamespace(
            detect_resolved=lambda msg: False,
            detect_unresolved=lambda msg: False,
            resolve_latest=lambda **k: None,
            add_issue=lambda *a, **k: None,
            get_open_issues_injection=lambda **k: "",
        )
        self.slang = SimpleNamespace(
            parse_correct=lambda msg: None,
            parse_teach=lambda msg: None,
            list_terms=lambda **k: [],
            set_scope=lambda *a, **k: 0,
            delete_term=lambda *a, **k: 0,
            detect_link_followup=lambda *a, **k: False,
            infer_candidate=lambda *a, **k: _noop(),
            get_slang_injection=lambda *a, **k: "",
        )
        self.mood = SimpleNamespace(
            detect_mood_name=lambda msg: None,
            record_mood=lambda *a, **k: None,
            detect_mood=lambda *a, **k: "",
            get_state_injection=lambda **k: "",
        )
        self.self_state = SimpleNamespace(get_self_state_injection=lambda **k: "")
        self.initiative = SimpleNamespace(mark_responded=lambda: None)
        self.plain_text = SimpleNamespace(
            has_markdown=lambda text: False,
            strip_markdown=lambda text: text,
        )
        self.sanitize = SimpleNamespace(sanitize=lambda text: text)
        self.request_trace = SimpleNamespace(record=lambda *a, **k: True)


async def _noop():
    return None


class _LLM:
    def __init__(self, reply="好的。"):
        self.reply = reply
        self.calls = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def make_runtime(llm=None):
    return ChatRuntime(
        settings=settings,
        llm=llm or _LLM(),
        memory=memory_module,
        knowledge=knowledge_module,
        services=_Services(),
        bg_tasks=set(),
        logger=logging.getLogger("assistant.chat.test"),
    )


def make_ctx(message, uid="", is_owner=True):
    return ChatContext(
        request=type("Request", (), {"state": type("State", (), {})()})(),
        request_model=ChatRequest(message=message),
        message=message,
        uid=uid,
        is_owner=is_owner,
    )


def test_command_short_circuit_skips_llm(db_env, monkeypatch):
    """命令命中（记录：…）直接返回，不调 LLM。"""
    llm = _LLM()
    runtime = make_runtime(llm=llm)
    ctx = make_ctx("记录：测试命令短路")
    resp = asyncio.run(run_chat(ctx, runtime))
    assert "已记录" in resp.reply
    assert llm.calls == []


def test_llm_failure_returns_friendly_reply(db_env, monkeypatch):
    """LLM 失败：用户消息已入库，assistant 侧不写，返回友好文案。"""
    llm = _LLM(reply=RuntimeError("backend down"))
    runtime = make_runtime(llm=llm)
    ctx = make_ctx("你好")
    resp = asyncio.run(run_chat(ctx, runtime))
    assert "连不上大脑" in resp.reply

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT sender FROM memories WHERE content='你好'"
        ).fetchall()
        assert rows, "用户消息应已入库"
        assert all(row["sender"] == "user" for row in rows)
        ai_rows = conn.execute(
            "SELECT id FROM memories WHERE sender='assistant'"
        ).fetchall()
        assert ai_rows == [], "LLM 失败时 assistant 消息不得入库"
    finally:
        conn.close()


def test_generation_retry_then_success(db_env, monkeypatch):
    """长文生成：首次失败自动重试一次，重试成功后正常返回。"""
    calls = {"n": 0}

    class _Flaky:
        async def chat(self, messages, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("timeout")
            assert kwargs.get("timeout") == 240 and kwargs.get("max_tokens") == 6000
            assert kwargs.get("model") == settings.novel_llm_model
            return "生成的长文"

    runtime = make_runtime(llm=_Flaky())
    ctx = make_ctx("继续写第三章")
    resp = asyncio.run(run_chat(ctx, runtime))
    assert resp.reply == "生成的长文"
    assert calls["n"] == 2

    conn = connect()
    try:
        row = conn.execute(
            "SELECT id FROM memories WHERE sender='assistant' AND content='生成的长文'"
        ).fetchone()
        assert row is not None, "重试成功的长文要正常入库"
    finally:
        conn.close()


def test_generation_double_failure_not_persisted(db_env, monkeypatch):
    """长文两次失败：返回重试提示，且不得把错误文案当 assistant 回复入库。"""
    llm = _LLM(reply=RuntimeError("timeout"))
    runtime = make_runtime(llm=llm)
    ctx = make_ctx("继续写第三章")
    resp = asyncio.run(run_chat(ctx, runtime))
    assert "长文生成连续两次失败" in resp.reply

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT content FROM memories WHERE sender='assistant'"
        ).fetchall()
        assert rows == [], "两次失败后 assistant 侧不得入库任何文本"
    finally:
        conn.close()


def test_user_and_assistant_messages_persisted(db_env, monkeypatch):
    llm = _LLM(reply="记下了")
    runtime = make_runtime(llm=llm)
    ctx = make_ctx("我项目代号叫青鸾")
    resp = asyncio.run(run_chat(ctx, runtime))
    assert resp.reply == "记下了"

    conn = connect()
    try:
        senders = {
            row["sender"]
            for row in conn.execute(
                "SELECT sender FROM memories WHERE content LIKE '%青鸾%' "
                "OR content='记下了'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert {"user", "assistant"} <= senders


def test_message_length_limit_guest(db_env, monkeypatch):
    """访客消息超长：直接拒绝且不调 LLM（设置显式支持该上限字段）。"""
    import pydantic
    from pydantic import ValidationError

    llm = _LLM()
    runtime = make_runtime(llm=llm)
    # 用超长访客消息 + 默认 2000 上限验证行为（不 monkeypatch 不存在的字段）
    ctx = make_ctx("超" * (2001), uid="10086", is_owner=False)
    resp = asyncio.run(run_chat(ctx, runtime))
    assert "消息太长啦" in resp.reply
    assert llm.calls == []
    del ValidationError, pydantic


def test_message_length_limit_owner(db_env, monkeypatch):
    llm = _LLM()
    runtime = make_runtime(llm=llm)
    ctx = make_ctx("超" * (8001))
    resp = asyncio.run(run_chat(ctx, runtime))
    assert "消息太长啦" in resp.reply
    assert llm.calls == []
