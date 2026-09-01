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
    from app.api.chat import SYSTEM_PROMPT

    assert "默认 1-4 句" in SYSTEM_PROMPT
    assert "不要罗列、复述或显摆" in SYSTEM_PROMPT
    assert "绝口不提旧事" in SYSTEM_PROMPT
    assert "以画像为准" in SYSTEM_PROMPT
