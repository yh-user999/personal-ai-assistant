"""检索自愈一期回归：检测器、变体重搜、聚合提炼、类名登记与域路由联动。

立项案例：「炼神里面有哪些境界」——索引未覆盖"炼神"，书里写作"练神"。
验证：检测触发条件、静态词不误触、聚合提炼注入、动态登记后第二次判域。
"""

import pytest

from app.config import settings
from app.core import knowledge
from app.models.database import connect, init_db, reset_connections
from app.services import index_healer as healer
from app.services import knowledge_domain


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    reset_connections()
    init_db()
    yield
    reset_connections()


def _seed_novel_chunks(conn, rows):
    """rows: [(chunk_index, content)]。同步写 FTS 行（gram 化与 knowledge 同源）。"""
    for idx, content in rows:
        cur = conn.execute(
            "INSERT INTO knowledge_chunks (doc_name, chunk_index, content, created_at) "
            "VALUES ('小说-寂静杀戮', ?, ?, ?)",
            (idx, content, "2026-09-01T00:00:00+00:00"),
        )
        conn.execute(
            "UPDATE knowledge_chunks SET domain='novel' WHERE id=?", (cur.lastrowid,)
        )
        conn.execute(
            "INSERT INTO knowledge_fts (chunk_id, grams) VALUES (?, ?)",
            (cur.lastrowid, knowledge._grams_text(content)),
        )
    conn.commit()


# ── 纯函数：触发与抽取 ──────────────────────────────────────

def test_detect_enum_intent():
    assert healer.detect_enum_intent("炼神里面有哪些境界")
    assert healer.detect_enum_intent("武道境界和修道境界分别有哪几个")
    assert not healer.detect_enum_intent("炼神是谁")
    assert not healer.detect_enum_intent("今天吃什么")


def test_extract_candidates():
    cands = healer.extract_candidates("炼神里面有哪些境界")
    assert "炼神" in cands
    cands2 = healer.extract_candidates("武道境界和修道境界分别有哪几个")
    assert "武道境界" in cands2 and "修道境界" in cands2
    assert healer.extract_candidates("今天吃什么") == []


def test_expand_variants():
    vs = healer.expand_variants("炼神")
    assert "炼神" in vs and "练神" in vs
    assert len(vs) <= 4


def test_core_word_missing():
    chunks = [{"content": "他在练气境界停留多年"}]
    assert healer.core_word_missing(["炼神"], chunks) is True   # 词面没出现
    assert healer.core_word_missing(["炼神"], [{"content": "炼神之后便是"}] ) is False
    assert healer.core_word_missing([], chunks) is False


# ── 诊断闸门 ────────────────────────────────────────────────

def test_diagnose_triggers_on_unrouted_enum(db_env):
    diag = healer.diagnose("炼神里面有哪些境界", [], [], [])
    assert diag and diag["action"] == "heal" and diag["unrouted"] is True


def test_diagnose_no_trigger_without_enum_intent(db_env):
    assert healer.diagnose("炼神是谁", [], [], []) is None


def test_diagnose_no_trigger_when_covered_and_hit(db_env):
    hits = [{"content": "命丛共有四种，分别是……"}]
    # 命丛在静态词表（实体索引已覆盖）→ 候选过滤后为空 → 不触发
    assert healer.diagnose("命丛有哪些", ["novel"], ["小说-寂静杀戮"], hits) is None


def test_diagnose_triggers_on_core_missing(db_env):
    # 已路由但核心词没出现在命中块 → 触发（换写法重搜的价值所在）
    hits = [{"content": "无关片段内容"}]
    diag = healer.diagnose("炼神有哪些境界", ["novel"], ["小说-寂静杀戮"], hits)
    assert diag and diag["core_missing"] is True


def test_diagnose_book_name_not_trigger(db_env):
    # 书名在覆盖词表里，不触发自愈（需有小说文档入库才有书名表）
    conn = connect()
    _seed_novel_chunks(conn, [(200, "寂静杀戮中的一段剧情。")])
    conn.close()
    assert healer.diagnose(
        "寂静杀戮里有哪几个境界", ["novel"], ["小说-寂静杀戮"], []
    ) is None


# ── 聚合检索与变体重搜 ──────────────────────────────────────

def test_aggregate_chunks_finds_variant(db_env):
    conn = connect()
    _seed_novel_chunks(conn, [
        (100, "练神境界，便是要学会心神力量。"),
        (101, "武道一途，先练气，后练神，再练虚。"),
        (102, "无关的剧情描述。"),
    ])
    conn.close()
    chunks = healer.aggregate_chunks(["炼神", "练神"])
    assert len(chunks) >= 2
    assert any("练神" in (c.get("content") or "") for c in chunks)


def test_classify_aggregate_domain(db_env):
    novel_chunks = [{"domain": "novel", "doc_name": "小说-X"}, {"domain": "novel", "doc_name": "小说-X"},
                    {"domain": "novel", "doc_name": "小说-X"}, {"domain": "manual", "doc_name": "教程"}]
    assert healer.classify_aggregate_domain(novel_chunks) == "novel"
    mixed = [{"domain": "manual", "doc_name": "教程"}, {"domain": "novel", "doc_name": "小说-X"}]
    assert healer.classify_aggregate_domain(mixed) == ""
    assert healer.classify_aggregate_domain([]) == ""


# ── 类名登记与域路由联动 ────────────────────────────────────

def test_register_class_then_domain_routing(db_env):
    assert knowledge_domain.register_class("炼神", domain="novel", source_query="测试") is True
    assert knowledge_domain.register_class("炼神", domain="novel") is False  # 幂等
    # 登记后 detect_domains 能判出 novel 域（第二次同类问题秒答的机制）
    domains, _docs = knowledge_domain.detect_domains("炼神有哪些境界")
    assert domains == ["novel"]
    # 未登记/无词仍判不出
    assert knowledge_domain.detect_domains("一个全新体系词有哪些") == ([], [])


def test_register_class_empty_domain_not_routed(db_env):
    knowledge_domain.register_class("某词", domain="", source_query="测试")
    domains, _docs = knowledge_domain.detect_domains("某词有哪些")
    assert domains == []  # domain='' 不参与路由，只做登记


# ── 集成：chat 主路径（mock LLM，走真实检索+自愈）───────────

def test_chat_heals_unrouted_enum_question(db_env, monkeypatch):
    """「炼神里面有哪些境界」：判不出域 → 触发 → 聚合提炼注入 → 登记类名。"""
    import app.api.chat as _chat_api
    import app.core.embedding as _embedding

    conn = connect()
    _seed_novel_chunks(conn, [
        (100, "练神境界，便是要学会心神力量。"),
        (101, "武道一途，先练气，后练神，再练虚。练虚又分显圣与造化。"),
        (102, "由虚入实，由实返虚，这是练虚武者追求的造化。"),
        (103, "他对武道境界的理解远超常人。"),
        (104, "练神之后，心神可与天地沟通。"),
    ])
    conn.close()

    replies = {"llm": [], "synthesized": None}

    async def fake_chat(messages, **kwargs):
        replies["llm"].append(messages)
        # 自愈的提炼调用 = system 里含"小说设定提炼器"
        if "小说设定提炼器" in (messages[0]["content"] or ""):
            replies["synthesized"] = True
            return "提炼结果：武道分练气、练神、练虚，练虚内分显圣、造化。"
        return "好的。"

    monkeypatch.setattr(_chat_api.llm, "chat", fake_chat)

    async def fake_embed(texts):
        return [[0.0] * settings.embedding_dimension for _ in texts]

    monkeypatch.setattr(_embedding, "embed", fake_embed)

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/chat", json={"message": "炼神里面有哪些境界"})
        assert r.status_code == 200

    # 提炼调用发生过（自愈生效）
    assert replies["synthesized"] is True
    # LLM 主回复的 system prompt 里注入了提炼结果
    healed_msgs = [
        m for m in replies["llm"]
        if any(x.get("role") == "system" and "知识库聚合资料" in x.get("content", "") for x in m)
    ]
    assert healed_msgs, "提炼结果未以独立 system 消息注入"
    # 类名已登记（第二次直接判域）
    domains, _ = knowledge_domain.detect_domains("炼神有哪些境界")
    assert domains == ["novel"]


def test_chat_healer_gate_disabled(db_env, monkeypatch):
    """HEALER_ENABLED=false：自愈完全不触发。"""
    import app.api.chat as _chat_api
    import app.core.embedding as _embedding

    monkeypatch.setattr(settings, "healer_enabled", False)

    async def fake_chat(messages, **kwargs):
        return "好的。"

    monkeypatch.setattr(_chat_api.llm, "chat", fake_chat)

    async def fake_embed(texts):
        return [[0.0] * settings.embedding_dimension for _ in texts]

    monkeypatch.setattr(_embedding, "embed", fake_embed)

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/chat", json={"message": "炼神里面有哪些境界"})
        assert r.status_code == 200

    conn = connect()
    n = conn.execute("SELECT COUNT(*) AS c FROM dynamic_classes").fetchone()["c"]
    conn.close()
    assert n == 0


def test_chat_guest_does_not_heal(db_env, monkeypatch):
    """访客不烧自愈预算：不提炼、不登记。"""
    import app.api.chat as _chat_api
    import app.core.embedding as _embedding

    conn = connect()
    _seed_novel_chunks(conn, [
        (100, "练神境界，便是要学会心神力量。"),
        (101, "武道一途，先练气，后练神，再练虚。"),
        (102, "练虚又分显圣与造化。"),
        (103, "武道境界之说。"),
    ])
    conn.close()

    async def fake_chat(messages, **kwargs):
        return "好的。"

    monkeypatch.setattr(_chat_api.llm, "chat", fake_chat)

    async def fake_embed(texts):
        return [[0.0] * settings.embedding_dimension for _ in texts]

    monkeypatch.setattr(_embedding, "embed", fake_embed)

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/chat",
                        json={"message": "炼神里面有哪些境界", "user_id": "10002"})
        assert r.status_code == 200

    conn = connect()
    n = conn.execute("SELECT COUNT(*) AS c FROM dynamic_classes").fetchone()["c"]
    conn.close()
    assert n == 0  # 访客不登记类名
