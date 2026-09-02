"""回复质量调优回归（v0.4.1）：防啰嗦、防复述、画像/事实口径统一。

背景：用户反馈"回复啰嗦、知道的东西都要提一遍、画像混乱"。
措施：①课程进度事实聚合（14条→1条）②记忆注入每条截断120字
③facts 上限 64→40 ④画像注入截断120字+刷新约束 ⑤系统提示加
"默认1-4句、不罗列背景、无强相关不提旧事、冲突以画像为准"。
"""
from datetime import datetime, timezone

import pytest

from app.config import settings
from app.core import memory
from app.models.database import connect, init_db, reset_connections


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    reset_connections()
    init_db()
    yield
    reset_connections()


# ── 1. 课程进度事实聚合 ────────────────────────────────────

def test_progress_facts_aggregated(db_env):
    """老式 14 条"第X课"事实 → 刷新后聚合为单条，口径更新到当前进度。"""
    from app.services.progress_sync import refresh_progress_facts

    conn = connect()
    now = datetime.now(timezone.utc).isoformat()
    # 造 v0.4 时代的老式事实（第8课还是"待开始"的过时口径）
    conn.execute(
        "INSERT INTO facts (user_id, subject, predicate, object, updated_at) VALUES ('owner','第6课','状态','测试工程与CI待开始',?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO facts (user_id, subject, predicate, object, updated_at) VALUES ('owner','第8课','状态','QQ私聊接入待开始',?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO facts (user_id, subject, predicate, object, updated_at) VALUES ('owner','项目','知识库','已入库两本小说',?)",
        (now,),
    )
    conn.commit()
    conn.close()

    n = refresh_progress_facts()
    assert n == 4  # 六课带教计划 + 扩展课程进度 + 项目×2

    conn = connect()
    rows = conn.execute(
        "SELECT subject, object FROM facts WHERE user_id='owner' ORDER BY id"
    ).fetchall()
    conn.close()
    subjects = [r["subject"] for r in rows]
    # 老式逐课事实被清理
    assert "第6课" not in subjects and "第8课" not in subjects
    # 聚合条目存在且口径已更新（第8课已完成）
    agg = [r["object"] for r in rows if r["subject"] == "扩展课程进度"]
    assert agg and "第8课QQ私聊接入已完成" in agg[0]
    # 项目类事实保留
    assert any(r["subject"] == "项目" and "已入库两本小说" in r["object"] for r in rows)


# ── 2. 注入截断 ────────────────────────────────────────────

def test_format_injection_truncates():
    mems = [{"ts": "2026-08-30T00:00:00+00:00", "content": "长" * 300, "summary": ""}]
    text = memory.format_injection(mems)
    assert len(text) < 160  # 120 字内容 + 前缀


def test_get_facts_injection_default_limit():
    """默认上限收紧为 40。"""
    import inspect

    sig = inspect.signature(memory.get_facts_injection)
    assert sig.parameters["limit"].default == 40


def test_profile_injection_truncates(db_env):
    from app.services.profile import get_profile_injection

    conn = connect()
    conn.execute(
        "INSERT INTO profile (user_id, dimension, value, confidence, updated_at) "
        "VALUES ('owner','project_info', ?, 0.9, '2026-08-30T00:00:00+00:00')",
        ("很" * 300,),
    )
    conn.commit()
    conn.close()
    text = get_profile_injection(user_id="owner")
    assert len(text) < 160  # 120 字值 + 维度前缀


def test_concerns_injection_limit():
    import inspect

    from app.services.concern_tracker import get_concerns_injection

    sig = inspect.signature(get_concerns_injection)
    assert sig.parameters["limit"].default == 4


# ── 3. 系统提示防啰嗦规则 ──────────────────────────────────

def test_system_prompt_anti_verbose_rules():
    """防啰嗦护栏：判据从"字数封顶"改为"相关性 + 不复述"。

    原来锁的是"默认 1-4 句"。实测这条管过头了——用户报的训练方案有肌群
    恢复冲突，属于"不说才是失职"的信息，却因为"用户没问的一律不主动展开"
    被压制，她只回了一句"12次还是你有别的想法？"把判断推回去。
    现在按信息价值分级：该压制的是复述背景与罗列知识，不是必要的风险提示。
    """
    from app.api.chat import SYSTEM_PROMPT

    # 相关性判断取代字数封顶
    assert "备查材料" in SYSTEM_PROMPT, "缺「注入资料是备查材料而非待播报清单」的定性"
    assert "只取与当前" in SYSTEM_PROMPT, "缺「只取相关部分」的约束"
    assert "1-3 句" in SYSTEM_PROMPT, "闲聊仍应有简短要求"
    # 仍要压制的三类啰嗦
    assert "不铺垫" in SYSTEM_PROMPT and "不客套" in SYSTEM_PROMPT
    assert "绝口不提旧事" in SYSTEM_PROMPT
    assert "以画像为准" in SYSTEM_PROMPT


def test_system_prompt_proactive_judgement():
    """主动判断规则：不许把判断推回用户。"""
    from app.api.chat import SYSTEM_PROMPT

    assert "不说才是失职" in SYSTEM_PROMPT
    assert "把判断推回给用户" in SYSTEM_PROMPT, "缺「禁止用『你觉得呢』推回」的约束"
    assert "替代方案" in SYSTEM_PROMPT


def test_generation_intent_detection():
    """长文生成档的意图判定：章节/续写/字数要求命中，普通问题不命中。"""
    from app.api.chat import _GENERATION_INTENT

    assert _GENERATION_INTENT.search("你生成第一章要大于3000，生成一个文件")
    assert _GENERATION_INTENT.search("继续")
    assert _GENERATION_INTENT.search("接着写")
    assert _GENERATION_INTENT.search("写第二章")
    assert _GENERATION_INTENT.search("字数要多一点，5000字左右")
    assert not _GENERATION_INTENT.search("今天有什么安排")
    assert not _GENERATION_INTENT.search("继续昨天的话题聊聊命丛")


def test_continuation_injects_full_last_ai(db_env, monkeypatch):
    """生成档的"继续"：上一条完整回复注入用户消息作上文（历史只截 500 字）。"""
    import app.api.chat as _chat_api
    import app.core.embedding as _embedding

    chapter = "第一章 灰烬里醒来。" + "李羽在疼痛中苏醒。" * 60  # >500 字
    conn = connect()
    conn.execute(
        "INSERT INTO memories (user_id, sender, content, ts) "
        "VALUES ('owner', 'assistant', ?, '2026-09-02T00:00:00+00:00')",
        (chapter,),
    )
    conn.commit()
    conn.close()

    captured = {}

    async def fake_chat(messages, **kwargs):
        captured["messages"] = messages
        return "后续内容。"

    async def fake_embed(texts):
        return [[0.0] * settings.embedding_dimension for _ in texts]

    import app.api.chat as _chat_mod
    monkeypatch.setattr(_chat_mod.llm, "chat", fake_chat)
    monkeypatch.setattr(_embedding, "embed", fake_embed)

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/chat", json={"message": "继续"})
        assert r.status_code == 200
    msgs = captured["messages"]
    last = msgs[-1]
    assert last["role"] == "user"
    assert "接续要求" in last["content"]
    assert chapter[:50] in last["content"]  # 完整上文注入
    assert "继续" in last["content"]


def test_generation_autoretry_once(db_env, monkeypatch):
    """生成档失败自动重试一次；普通请求不重试。"""
    import app.api.chat as _chat_api
    import app.core.embedding as _embedding

    calls = {"n": 0}

    async def fake_chat(messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("模拟首次超时")
        return "重试成功的章节内容。" * 20

    monkeypatch.setattr(_chat_api.llm, "chat", fake_chat)

    async def fake_embed(texts):
        return [[0.0] * settings.embedding_dimension for _ in texts]

    monkeypatch.setattr(_embedding, "embed", fake_embed)

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/chat", json={"message": "写第一章"})
        assert r.status_code == 200
        assert "重试成功" in r.json()["reply"]
    assert calls["n"] == 2  # 失败 1 次 + 重试 1 次
