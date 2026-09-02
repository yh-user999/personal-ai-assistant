"""黑话模块回归（一至三期）：教学/纠正/注入/共享隔离/转正/淘汰/语境推断。"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models.database import connect, init_db, reset_connections
from app.services import slang


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    reset_connections()
    init_db()
    yield
    reset_connections()


# ── 一期：解析与存储 ───────────────────────────────────────

def test_parse_teach():
    assert slang.parse_teach("记黑话：鸡蛋 = 链接里的免费token福利") == ("鸡蛋", "链接里的免费token福利")
    assert slang.parse_teach("黑话「鸡蛋」指链接里的白嫖福利") == ("鸡蛋", "链接里的白嫖福利")
    assert slang.parse_teach("今天吃什么") is None


def test_parse_correct():
    assert slang.parse_correct("不对，鸡蛋是免费token的意思") == ("鸡蛋", "免费token")
    assert slang.parse_correct("刚才说的鸡蛋是指白嫖福利") == ("鸡蛋", "白嫖福利")
    assert slang.parse_correct("今天天气不错") is None


def test_owner_teach_goes_shared(db_env):
    slang.save_term("鸡蛋", "链接里的白嫖福利", user_id=None)
    conn = connect()
    row = conn.execute("SELECT scope, status FROM slang_terms WHERE term='鸡蛋'").fetchone()
    conn.close()
    assert row["scope"] == "shared" and row["status"] == "confirmed"


def test_guest_teach_goes_private(db_env):
    slang.save_term("鸡蛋", "我家鸡下的蛋", user_id="10002")
    conn = connect()
    row = conn.execute("SELECT scope FROM slang_terms WHERE term='鸡蛋'").fetchone()
    conn.close()
    assert row["scope"] == "private"


# ── 注入与共享隔离 ─────────────────────────────────────────

def test_injection_owner_sees_own(db_env):
    slang.save_term("鸡蛋", "链接里的白嫖福利", user_id=None,
                    source_episode="链接 + 还有鸡蛋")
    text = slang.get_slang_injection("今天还有鸡蛋吗", user_id=None)
    assert "「鸡蛋」" in text and "白嫖福利" in text and "出处" in text


def test_injection_guest_sees_owner_shared(db_env):
    slang.save_term("鸡蛋", "链接里的白嫖福利", user_id=None)
    slang.save_term("面包", "访客自己的黑话", user_id="10002")
    text = slang.get_slang_injection("还有鸡蛋吗", user_id="10002")
    assert "「鸡蛋」" in text          # 主人的 shared 对访客可见
    assert "「面包」" not in text      # 词面没出现
    # 访客自己命中
    text2 = slang.get_slang_injection("给我面包", user_id="10002")
    assert "「面包」" in text2


def test_injection_guest_private_hidden_from_owner(db_env):
    slang.save_term("暗号甲", "访客私密说法", user_id="10002")
    assert slang.get_slang_injection("暗号甲是什么", user_id=None) == ""


def test_no_hit_no_injection(db_env):
    slang.save_term("鸡蛋", "链接福利", user_id=None)
    assert slang.get_slang_injection("今天吃什么", user_id=None) == ""


def test_single_char_term_never_injects(db_env):
    slang.save_term("蛋", "单字不该注入", user_id=None)
    assert slang.get_slang_injection("一个蛋", user_id=None) == ""


# ── 二期：转正状态机与语义兜底 ─────────────────────────────

def test_candidate_promotes_after_two_uses(db_env):
    slang.save_term("鸡蛋", "链接里的白嫖福利", user_id=None, status="candidate")
    slang.get_slang_injection("还有鸡蛋吗", user_id=None)   # 第 1 次使用
    conn = connect()
    row = conn.execute("SELECT status, use_count FROM slang_terms WHERE term='鸡蛋'").fetchone()
    conn.close()
    assert row["status"] == "candidate" and row["use_count"] == 1
    slang.get_slang_injection("还有鸡蛋吗", user_id=None)   # 第 2 次 → 转正
    conn = connect()
    row = conn.execute("SELECT status, use_count FROM slang_terms WHERE term='鸡蛋'").fetchone()
    conn.close()
    assert row["status"] == "confirmed" and row["use_count"] == 2


def test_meaning_question_semantic_fallback(db_env):
    slang.save_term("鸡蛋", "链接里的白嫖福利", user_id=None)
    text = slang.get_slang_injection("鸡蛋是啥意思", user_id=None)
    assert "「鸡蛋」" in text  # 词面命中本身
    # 反查：term 出现在提问主体里
    text2 = slang.get_slang_injection("鸡蛋的意思是什么", user_id=None)
    assert "「鸡蛋」" in text2


def test_link_followup_detection():
    assert slang.detect_link_followup("https://example.com/token 这个链接", "还有鸡蛋")
    assert not slang.detect_link_followup("https://example.com/token", "这个链接里有什么内容可以详细解释一下")  # 超长
    assert not slang.detect_link_followup("", "还有鸡蛋")
    assert not slang.detect_link_followup("今天天气不错", "还有鸡蛋")
    assert not slang.detect_link_followup("https://a.com", "https://b.com")


def test_infer_candidate_background(db_env, monkeypatch):
    """语境推断：mock LLM 返回黑话 JSON → 存 candidate（不打扰用户）。"""
    import app.services.slang as _slang_mod

    async def fake_chat_json(system, user, **kw):
        return {"is_slang": True, "term": "鸡蛋", "meaning": "链接里的免费token福利"}

    monkeypatch.setattr(_slang_mod.llm, "chat_json", fake_chat_json)
    assert asyncio.run(_slang_mod.infer_candidate(
        "https://example.com/token", "还有鸡蛋", user_id=None
    )) is True
    conn = connect()
    row = conn.execute("SELECT scope, status, source_episode FROM slang_terms WHERE term='鸡蛋'").fetchone()
    conn.close()
    assert row["scope"] == "shared" and row["status"] == "candidate"
    assert "链接" in row["source_episode"]


def test_infer_not_slang_no_write(db_env, monkeypatch):
    import app.services.slang as _slang_mod

    async def fake_chat_json(system, user, **kw):
        return {"is_slang": False}

    monkeypatch.setattr(_slang_mod.llm, "chat_json", fake_chat_json)
    assert asyncio.run(_slang_mod.infer_candidate(
        "https://example.com", "好的谢谢", user_id=None
    )) is False
    conn = connect()
    n = conn.execute("SELECT COUNT(*) c FROM slang_terms").fetchone()["c"]
    conn.close()
    assert n == 0


# ── 三期：管理命令与淘汰 ───────────────────────────────────

def test_scope_management(db_env):
    slang.save_term("鸡蛋", "链接福利", user_id=None, scope="shared")
    assert slang.set_scope("鸡蛋", "private", user_id=None) == 1
    assert slang.set_scope("鸡蛋", "shared", user_id=None) == 1
    assert slang.set_scope("不存在", "shared", user_id=None) == 0


def test_delete_term(db_env):
    slang.save_term("鸡蛋", "链接福利", user_id=None)
    assert slang.delete_term("鸡蛋", user_id=None) == 1
    assert slang.delete_term("鸡蛋", user_id=None) == 0


def test_evict_stale_slang(db_env):
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    conn = connect()
    conn.execute(
        "INSERT INTO slang_terms (user_id, term, meaning, scope, status, use_count, created_at, updated_at) "
        "VALUES ('owner','过期候选','旧意思','shared','candidate',0,?,?)", (old, old)
    )
    conn.execute(
        "INSERT INTO slang_terms (user_id, term, meaning, scope, status, use_count, created_at, updated_at) "
        "VALUES ('owner','低使用确认','旧意思','shared','confirmed',0,?,?)", (old, old)
    )
    conn.execute(
        "INSERT INTO slang_terms (user_id, term, meaning, scope, status, use_count, created_at, updated_at) "
        "VALUES ('owner','常用词','旧意思','shared','confirmed',50,?,?)", (old, old)
    )
    conn.commit()
    conn.close()
    result = slang.evict_stale_slang()
    assert result["deleted_candidates"] == 1 and result["demoted_confirmed"] == 1
    conn = connect()
    rows = {r["term"]: r["status"] for r in conn.execute("SELECT term, status FROM slang_terms").fetchall()}
    conn.close()
    assert "过期候选" not in rows
    assert rows["低使用确认"] == "candidate"
    assert rows["常用词"] == "confirmed"


# ── chat 集成 ──────────────────────────────────────────────

@pytest.fixture
def chat_env(db_env, monkeypatch):
    import app.api.chat as _chat_api
    import app.core.embedding as _embedding

    async def fake_chat(messages, **kwargs):
        return "好的。"

    monkeypatch.setattr(_chat_api.llm, "chat", fake_chat)

    async def fake_embed(texts):
        return [[0.0] * settings.embedding_dimension for _ in texts]

    monkeypatch.setattr(_embedding, "embed", fake_embed)
    yield


def test_chat_teach_and_inject(chat_env, monkeypatch):
    """主人记黑话 → 落 shared；随后提问注入（不烧 LLM 验证教学分支）。"""
    import app.api.chat as _chat_api

    systems = []

    async def fake_chat(messages, **kwargs):
        systems.append(messages[0]["content"])
        return "好的。"

    monkeypatch.setattr(_chat_api.llm, "chat", fake_chat)

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/chat", json={"message": "记黑话：鸡蛋 = 链接里的免费token福利"})
        assert "记下黑话" in r.json()["reply"]
        client.post("/api/chat", json={"message": "今天还有鸡蛋吗"})
    assert any("「鸡蛋」" in s for s in systems), "黑话未注入后续对话的 system prompt"


def test_chat_guest_teach_private(chat_env):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/chat", json={"message": "记黑话：鸡蛋 = 我家鸡下的蛋", "user_id": "10002"})
        assert "仅你自己可见" in r.json()["reply"]
    conn = connect()
    row = conn.execute("SELECT scope FROM slang_terms WHERE term='鸡蛋'").fetchone()
    conn.close()
    assert row["scope"] == "private"


def test_chat_guest_cannot_share(chat_env):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        # 访客发"黑话共享：X"没有对应 handler 分支（is_owner=False）→ 走 LLM 路径
        client.post("/api/chat", json={"message": "记黑话：鸡蛋 = 访客的说法", "user_id": "10002"})
        client.post("/api/chat", json={"message": "黑话共享：鸡蛋", "user_id": "10002"})
    conn = connect()
    row = conn.execute("SELECT scope FROM slang_terms WHERE term='鸡蛋' AND user_id='10002'").fetchone()
    conn.close()
    assert row["scope"] == "private"  # 未被共享
