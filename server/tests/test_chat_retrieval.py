"""聊天检索层测试：主人/访客数据隔离、访客不触知识库、自愈降级、请求级状态。"""
import asyncio

import pytest

from app.chat.context import ChatContext, ChatRequest, ChatRuntime
from app.chat import retrieval
from app.config import settings
from app.core import memory as memory_module
from app.models.database import init_db, reset_connections


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    reset_connections()
    init_db()
    yield
    reset_connections()


ALL_SERVICE_NAMES = (
    "cooccurrence",
    "subjective_time",
    "knowledge_domain",
    "index_healer",
    "novel_entities",
    "fitness",
    "profile",
    "self_reflect",
    "identity_guard",
    "fact_extract",
    "concern_tracker",
    "jargon",
    "few_shot",
    "behavior_context",
    "goals",
    "unresolved",
    "slang",
    "mood",
    "self_state",
    "growth",
    "intent_goals",
    "knowledge_hint",
)


class _Stub:
    """访客/隔离路径不允许触达的 service：任何方法调用都直接失败。"""

    def __getattr__(self, name):
        raise AssertionError(f"该路径不应调用 service 方法 {name}")


class _GuestSafe:
    """访客路径允许触达的注入类方法：全部返回空值。"""

    def expand(self, mems, **k):
        return mems

    def format_injection(self, mems):
        return ""

    def get_profile_injection(self, **k):
        return ""

    def get_concerns_injection(self, **k):
        return ""

    def get_jargon_injection(self, *a, **k):
        return ""

    def detect_definition(self, *a, **k):
        return None

    def get_examples_injection(self, **k):
        return ""

    def detect_positive_feedback(self, *a, **k):
        return False

    def get_goals_injection(self, **k):
        return ""

    def get_open_issues_injection(self, **k):
        return ""

    def get_slang_injection(self, *a, **k):
        return ""

    def detect_mood(self, *a, **k):
        return ""

    def get_state_injection(self, **k):
        return ""

    def get_self_state_injection(self, **k):
        return ""


class _OwnerSafe:
    """主人路径的通用空值桩：覆盖 retrieve/prepare_turn 用到的方法。"""

    def expand(self, mems, **k):
        return mems

    def format_injection(self, mems):
        return ""

    def detect_domains(self, *a, **k):
        return [], []

    def register_class(self, *a, **k):
        return None

    def detect_enum_intent(self, *a, **k):
        return False

    def diagnose(self, *a, **k):
        return None

    async def heal(self, *a, **k):
        return "", []

    def classify_aggregate_domain(self, *a, **k):
        return ""

    def majority_novel_book(self, *a, **k):
        return ""

    def build_entity_context(self, *a, **k):
        return ""

    def get_fitness_facts(self, *a, **k):
        return []

    def get_profile_injection(self, **k):
        return ""

    def detect_correction(self, *a, **k):
        return False

    def classify_lesson(self, *a, **k):
        return "fact"

    def save_lesson(self, *a, **k):
        return 0

    def get_lessons_injection(self, **k):
        return ""

    def is_roleplay_or_insult(self, *a, **k):
        return False

    def get_concerns_injection(self, **k):
        return ""

    def get_jargon_injection(self, *a, **k):
        return ""

    def detect_definition(self, *a, **k):
        return None

    def save_term(self, *a, **k):
        return None

    def get_examples_injection(self, **k):
        return ""

    def detect_positive_feedback(self, *a, **k):
        return False

    def save_example(self, *a, **k):
        return 0

    def get_goals_injection(self, **k):
        return ""

    def get_open_issues_injection(self, **k):
        return ""

    def get_slang_injection(self, *a, **k):
        return ""

    def detect_mood(self, *a, **k):
        return ""

    def detect_mood_name(self, *a, **k):
        return None

    def get_state_injection(self, **k):
        return ""

    def get_self_state_injection(self, **k):
        return ""

    def detect_self_doubt(self, *a, **k):
        return False

    def build_injection(self, **k):
        return ""

    def record_intent(self, *a, **k):
        return None

    def build_hint(self, *a, **k):
        return ""

    async def maybe_extract_facts(self, *a, **k):
        return None

    async def extract_from_last_ai(self, *a, **k):
        return None

    def is_record_command(self, *a, **k):
        return False

    def is_short_confirm(self, *a, **k):
        return False


def make_runtime(services_map, knowledge=None):
    import logging

    from types import SimpleNamespace

    from app.core import knowledge as knowledge_module
    from app.core import llm

    missing = set(ALL_SERVICE_NAMES) - set(services_map)
    assert not missing, f"缺少 service 桩: {missing}"
    return ChatRuntime(
        settings=settings,
        llm=llm,
        memory=memory_module,
        knowledge=knowledge or knowledge_module,
        services=SimpleNamespace(**services_map),
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


def _patch_memory_scoped(monkeypatch, expect_uid=None):
    async def fake_search(query, **kwargs):
        if expect_uid is not None:
            assert kwargs.get("user_id") == expect_uid, "记忆检索必须显式限定 uid"
        return []

    monkeypatch.setattr(memory_module, "search", fake_search)
    monkeypatch.setattr(memory_module, "deep_keyword_search", lambda *a, **k: [])
    monkeypatch.setattr(memory_module, "get_recent_history", lambda *a, **k: [])
    monkeypatch.setattr(memory_module, "get_older_summaries", lambda *a, **k: [])
    monkeypatch.setattr(memory_module, "get_facts_injection", lambda **k: "")


def test_guest_retrieve_skips_owner_knowledge_and_lessons(db_env, monkeypatch):
    """访客检索不触主人知识库/教训/行为/情绪；记忆检索限定访客 uid。"""
    _patch_memory_scoped(monkeypatch, expect_uid="10086")

    services_map = {name: _Stub() for name in ALL_SERVICE_NAMES}
    safe = _GuestSafe()
    for name in (
        "cooccurrence",
        "subjective_time",
        "profile",
        "concern_tracker",
        "jargon",
        "few_shot",
        "goals",
        "unresolved",
        "slang",
        "self_state",
        "mood",
    ):
        services_map[name] = safe

    runtime = make_runtime(services_map)
    ctx = make_ctx("你好呀", uid="10086", is_owner=False)
    preparation = retrieval.prepare_turn(ctx, runtime)
    bundle = asyncio.run(retrieval.retrieve(ctx, runtime, preparation))

    assert bundle.knowledge_text == ""
    assert bundle.healed_text == ""
    assert bundle.lessons == ""
    assert bundle.behavior == ""
    assert bundle.mood == ""
    assert bundle.mood_state == ""
    assert bundle.intent_label == ""
    assert bundle.trace["path"] == "hybrid"
    assert bundle.trace["routing"] == {}


def test_healer_failure_does_not_break_main_flow(db_env, monkeypatch):
    """自愈诊断抛异常 → 只记日志，主回复链路照常拿到 bundle。"""

    class _Knowledge:
        async def search_knowledge(self, *a, **k):
            return []

        def last_vector_degraded(self):
            return False

        def expand_chunks(self, hits, **k):
            return hits

        def format_knowledge_injection(self, hits):
            return ""

        def get_alias_note(self, *a, **k):
            return ""

        def get_novel_facts(self, *a, **k):
            return []

    class _BoomHealer(_OwnerSafe):
        def diagnose(self, *a, **k):
            raise RuntimeError("healer down")

    _patch_memory_scoped(monkeypatch)
    services_map = {name: _OwnerSafe() for name in ALL_SERVICE_NAMES}
    services_map["index_healer"] = _BoomHealer()
    runtime = make_runtime(services_map, knowledge=_Knowledge())
    monkeypatch.setattr(settings, "healer_enabled", True)

    ctx = make_ctx("命丛有哪些")
    preparation = retrieval.prepare_turn(ctx, runtime)
    bundle = asyncio.run(retrieval.retrieve(ctx, runtime, preparation))
    assert bundle.trace["degraded"] == 0
    assert bundle.trace["path"] == "hybrid"


def test_healed_hit_sets_intent_and_path(db_env, monkeypatch):
    """自愈命中 → intent_label=healed、path=heal、healer_words 记录。"""

    class _Knowledge:
        async def search_knowledge(self, *a, **k):
            return []

        def last_vector_degraded(self):
            return False

        def expand_chunks(self, hits, **k):
            return hits

        def format_knowledge_injection(self, hits):
            return ""

        def get_alias_note(self, *a, **k):
            return ""

        def get_novel_facts(self, *a, **k):
            return []

    class _Healer(_OwnerSafe):
        def diagnose(self, *a, **k):
            return {"words": ["命丛"]}

        async def heal(self, diagnosis, *a, **k):
            return "聚合资料文本", [{"content": "chunk"}]

    _patch_memory_scoped(monkeypatch)
    services_map = {name: _OwnerSafe() for name in ALL_SERVICE_NAMES}
    services_map["index_healer"] = _Healer()
    runtime = make_runtime(services_map, knowledge=_Knowledge())
    monkeypatch.setattr(settings, "healer_enabled", True)

    ctx = make_ctx("命丛有哪些")
    preparation = retrieval.prepare_turn(ctx, runtime)
    bundle = asyncio.run(retrieval.retrieve(ctx, runtime, preparation))
    assert bundle.intent_label == "healed"
    assert bundle.trace["path"] == "heal"
    assert bundle.trace["healer_words"] == ["命丛"]
    assert "不可信参考资料" in bundle.healed_text


def test_build_search_query_expands_followup_with_known_anchor():
    history = [
        {"role": "user", "content": "李羽在寂静杀戮里受伤了"},
        {"role": "assistant", "content": "李羽被左志诚击伤，后来恢复。"},
    ]
    query, anchors, expanded = retrieval.build_search_query(
        "他受了什么伤", history, known_anchors={"李羽", "寂静杀戮", "左志诚"}
    )
    assert expanded is True
    assert "他受了什么伤" in query
    assert "李羽" in query or "左志诚" in query
    assert anchors


def test_build_search_query_does_not_expand_confirm_or_new_domain():
    history = [{"role": "user", "content": "李羽的能力是什么"}]
    assert retrieval.build_search_query("谢谢", history, known_anchors={"李羽"}) == ("谢谢", [], False)
    assert retrieval.build_search_query("我的简历怎么改", history, known_anchors={"李羽"}) == ("我的简历怎么改", [], False)
    assert retrieval.build_search_query("服务器怎么部署", history, known_anchors={"李羽"}) == ("服务器怎么部署", [], False)


def test_build_search_query_non_followup_without_history_is_original():
    assert retrieval.build_search_query("天气不错", [], known_anchors={"李羽"}) == ("天气不错", [], False)


def test_last_query_vec_request_scoped_after_search(db_env, monkeypatch):
    """search 写入的 query 向量缓存是请求/任务级状态：take 后即失效。"""
    from app.core import embedding

    vec = [0.1] * settings.embedding_dimension

    async def fake_embed(texts):
        return [vec for _ in texts]

    monkeypatch.setattr(embedding, "embed", fake_embed)

    async def run():
        await memory_module.search("青鸾项目", top_k=2, user_id="")
        first = memory_module.take_query_vec("青鸾项目")
        second = memory_module.take_query_vec("青鸾项目")
        return first, second

    first, second = asyncio.run(run())
    assert first == vec
    assert second is None


def test_generation_retrieval_adds_sepia_only_for_owner(db_env, monkeypatch):
    class _Knowledge:
        async def search_knowledge(self, *a, **k):
            return []

        def last_vector_degraded(self):
            return False

        def expand_chunks(self, hits, **k):
            return hits

        def format_knowledge_injection(self, hits):
            return ""

        def get_alias_note(self, *a, **k):
            return ""

        def get_novel_facts(self, *a, **k):
            return []

    _patch_memory_scoped(monkeypatch)
    services_map = {name: _OwnerSafe() for name in ALL_SERVICE_NAMES}
    runtime = make_runtime(services_map, knowledge=_Knowledge())

    owner_ctx = make_ctx("继续写")
    owner_prep = retrieval.prepare_turn(owner_ctx, runtime)
    owner_bundle = asyncio.run(retrieval.retrieve(owner_ctx, runtime, owner_prep))
    assert any("Sepia 小说生成规则" in block for block in owner_bundle.extra_blocks)

    guest_ctx = make_ctx("继续写", uid="10086", is_owner=False)
    guest_prep = retrieval.prepare_turn(guest_ctx, runtime)
    guest_bundle = asyncio.run(retrieval.retrieve(guest_ctx, runtime, guest_prep))
    assert not any("Sepia 小说生成规则" in block for block in guest_bundle.extra_blocks)

    normal_ctx = make_ctx("你好")
    normal_prep = retrieval.prepare_turn(normal_ctx, runtime)
    normal_bundle = asyncio.run(retrieval.retrieve(normal_ctx, runtime, normal_prep))
    assert not any("Sepia 小说生成规则" in block for block in normal_bundle.extra_blocks)
