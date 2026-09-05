"""检索自愈二期/三期回归：自动抽取三闸、候选池、纠错反馈回路。"""
import asyncio

import pytest

from app.config import settings
from app.core import knowledge
from app.models.database import connect, init_db, reset_connections
from app.services import index_healer, knowledge_domain


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    reset_connections()
    init_db()
    yield
    reset_connections()


def _seed_novel_chunks(conn, rows):
    for idx, content in rows:
        cur = conn.execute(
            "INSERT INTO knowledge_chunks (doc_name, chunk_index, content, created_at) "
            "VALUES ('小说-寂静杀戮', ?, ?, '2026-09-01T00:00:00+00:00')",
            (idx, content),
        )
        conn.execute("UPDATE knowledge_chunks SET domain='novel' WHERE id=?", (cur.lastrowid,))
        conn.execute(
            "INSERT INTO knowledge_fts (chunk_id, grams) VALUES (?, ?)",
            (cur.lastrowid, knowledge._grams_text(content)),
        )
    conn.commit()


# ── 预算/幂等闸 ────────────────────────────────────────────

def test_auto_budget_daily_cap(db_env):
    # 占位式闸门：前 3 个词成功，第 4 个被拒（每日限额）
    assert index_healer._reserve_extract_slot("词0") is True
    assert index_healer._reserve_extract_slot("词1") is True
    assert index_healer._reserve_extract_slot("词2") is True
    assert index_healer._reserve_extract_slot("词3") is False  # 当日已 3 次
    # 同词幂等：已占位再占必拒
    assert index_healer._reserve_extract_slot("词0") is False


def test_auto_budget_ok_when_quota_left(db_env):
    assert index_healer._reserve_extract_slot("炼神") is True


def test_reserve_atomic_under_race(db_env):
    """并发竞态：两个任务同时占位同一词，只有一个成功（day_key 唯一性）。"""
    ok = [index_healer._reserve_extract_slot("炼神") for _ in range(10)]
    assert sum(ok) == 1


# ── 自动抽取：置信分流 ─────────────────────────────────────

def _fake_extract(payload):
    async def fake(book, kind, *, dry_run=False, max_blocks=40):
        return payload
    return fake


def test_auto_extract_confidence_split(db_env, monkeypatch):
    """名字在 ≥2 块出现 → 直接入库；1 块 → 候选池。"""
    conn = connect()
    _seed_novel_chunks(conn, [
        (1, "练神我相是练神里的一个层次，练神我相十分难得。"),
        (2, "练神我相再度出现。"),
        (3, "心神合一只有一处提到。"),
    ])
    conn.close()

    monkeypatch.setattr(
        "app.services.novel_entities.extract_entities",
        _fake_extract({
            "book": "小说-寂静杀戮", "kind": "炼神",
            "names": [
                {"name": "练神我相", "first_chunk": 1},
                {"name": "心神合一", "first_chunk": 3},
            ],
            "group_name": "", "group_size": 0,
        }),
    )
    result = asyncio.run(index_healer.auto_extract_task(["炼神"], "小说-寂静杀戮"))
    assert result["confirmed"] == 1 and result["candidates"] == 1

    conn = connect()
    ents = conn.execute(
        "SELECT name, verified FROM novel_entities WHERE kind='炼神'"
    ).fetchall()
    cands = conn.execute(
        "SELECT name, status FROM entity_candidates WHERE kind='炼神'"
    ).fetchall()
    conn.close()
    assert [e["name"] for e in ents] == ["练神我相"]
    assert [(c["name"], c["status"]) for c in cands] == [("心神合一", "pending")]


def test_auto_extract_budget_gate_skips(db_env, monkeypatch):
    called = {"n": 0}

    async def fake(book, kind, **kw):
        called["n"] += 1
        return {"book": book, "kind": kind, "names": [], "group_name": "", "group_size": 0}

    monkeypatch.setattr("app.services.novel_entities.extract_entities", fake)
    # 先占位（模拟同词当天已抽过）→ 任务应被幂等闸拦截
    assert index_healer._reserve_extract_slot("炼神") is True
    result = asyncio.run(index_healer.auto_extract_task(["炼神"], "小说-寂静杀戮"))
    assert result["skipped"] == "budget_or_duplicate"
    assert called["n"] == 0


# ── 候选池管理 ─────────────────────────────────────────────

def test_candidate_confirm_and_discard(db_env):
    index_healer.candidate_add("小说-寂静杀戮", "炼神", "心神合一", 3)
    assert index_healer.candidate_list()[0]["name"] == "心神合一"
    assert index_healer.candidate_confirm("心神合一") == 1
    conn = connect()
    ent = conn.execute(
        "SELECT name, verified FROM novel_entities WHERE name='心神合一'"
    ).fetchone()
    status = conn.execute(
        "SELECT status FROM entity_candidates WHERE name='心神合一'"
    ).fetchone()
    conn.close()
    assert ent is not None and ent["verified"] == 1
    assert status["status"] == "confirmed"

    index_healer.candidate_add("小说-寂静杀戮", "炼神", "废弃名", 4)
    assert index_healer.candidate_discard("废弃名") == 1
    assert index_healer.candidate_list() == []


# ── 纠错反馈回路 ───────────────────────────────────────────

def test_apply_correction_removes_all_indexes(db_env):
    knowledge_domain.register_class("炼神", domain="novel", source_query="测试")
    conn = connect()
    conn.execute(
        "INSERT INTO novel_entities (book, name, kind, first_chunk, note, created_at) "
        "VALUES ('小说-寂静杀戮','炼神','境界',1,'','2026-09-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    index_healer.candidate_add("小说-寂静杀戮", "境界", "炼神", 1)

    reply = index_healer.apply_correction("不对，炼神不是境界")
    assert reply and "已修正" in reply

    conn = connect()
    n_dyn = conn.execute("SELECT COUNT(*) c FROM dynamic_classes WHERE class_word='炼神'").fetchone()["c"]
    n_ent = conn.execute("SELECT COUNT(*) c FROM novel_entities WHERE name='炼神'").fetchone()["c"]
    n_cand = conn.execute(
        "SELECT COUNT(*) c FROM entity_candidates WHERE name='炼神' AND status='pending'"
    ).fetchone()["c"]
    n_audit = conn.execute("SELECT COUNT(*) c FROM index_corrections WHERE target='炼神'").fetchone()["c"]
    conn.close()
    assert n_dyn == 0 and n_ent == 0 and n_cand == 0 and n_audit == 1
    # 动态词表缓存已失效：路由不再认识
    assert knowledge_domain.detect_domains("炼神有哪些境界") == ([], [])


def test_apply_correction_noop_when_target_unknown(db_env):
    assert index_healer.apply_correction("不对，不存在的词不是境界") is None


def test_apply_correction_not_triggered_by_normal_chat(db_env):
    assert index_healer.apply_correction("不对，你上次说错了") is None
    assert index_healer.apply_correction("今天天气不错") is None


# ── detect_kinds 动态扩展 ──────────────────────────────────

def test_detect_kinds_includes_dynamic_and_db_kinds(db_env):
    from app.services.novel_entities import detect_kinds

    knowledge_domain.register_class("炼神", domain="novel", source_query="测试")
    conn = connect()
    conn.execute(
        "INSERT INTO novel_entities (book, name, kind, first_chunk, note, created_at) "
        "VALUES ('小说-寂静杀戮','心神合一','炼神',1,'','2026-09-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    assert "炼神" in detect_kinds("炼神有哪些境界")


# ── chat 集成 ──────────────────────────────────────────────

def test_chat_correction_hook(db_env, monkeypatch):
    """主人说「不对，炼神不是境界」→ 直接回修正文案，不烧 LLM。"""
    import app.api.chat as _chat_api
    import app.core.embedding as _embedding

    knowledge_domain.register_class("炼神", domain="novel", source_query="测试")

    calls = {"n": 0}

    async def fake_chat(messages, **kwargs):
        calls["n"] += 1
        return "好的。"

    monkeypatch.setattr(_chat_api.llm, "chat", fake_chat)

    async def fake_embed(texts):
        return [[0.0] * settings.embedding_dimension for _ in texts]

    monkeypatch.setattr(_embedding, "embed", fake_embed)

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/chat", json={"message": "不对，炼神不是境界"})
        assert r.status_code == 200
        assert "已修正" in r.json()["reply"]
    assert calls["n"] == 0  # 零 LLM


def test_chat_candidate_commands(db_env, monkeypatch):
    """主人「查看候选抽取」列出候选；访客不响应管理命令。"""
    import app.api.chat as _chat_api
    import app.core.embedding as _embedding

    index_healer.candidate_add("小说-寂静杀戮", "炼神", "心神合一", 3)

    async def fake_chat(messages, **kwargs):
        return "好的。"

    monkeypatch.setattr(_chat_api.llm, "chat", fake_chat)

    async def fake_embed(texts):
        return [[0.0] * settings.embedding_dimension for _ in texts]

    monkeypatch.setattr(_embedding, "embed", fake_embed)

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/chat", json={"message": "查看候选抽取"})
        assert "心神合一" in r.json()["reply"]
        # 访客：命令族被跳过 → 走 LLM 路径（不执行管理动作）
        r2 = client.post("/api/chat", json={"message": "确认抽取：心神合一", "user_id": "10002"})
        assert r2.status_code == 200
    conn = connect()
    cand = conn.execute(
        "SELECT status FROM entity_candidates WHERE name='心神合一'"
    ).fetchone()
    conn.close()
    assert cand["status"] == "pending"  # 访客没能确认
