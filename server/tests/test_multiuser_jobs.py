"""Phase 3 定时任务隔离回归：周报/日报/统计只读主人，访客记忆 30 天淘汰。

背景（v0.4 多人支持 Phase 3）：
- weekly_reflect / daily_summary / weekly_stats / top_topics 只统计主人数据
- evict_stale：主人低 importance 365 天淘汰（importance≥1 永久），
  访客记忆 30 天直接删（facts 层已留确认过的设定）
"""
import asyncio
from datetime import datetime, timedelta, timezone

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


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_evict_guest_30d_owner_kept(db_env):
    from app.services.analyzer import evict_stale

    conn = connect()
    # 访客 40 天前记忆 → 应被删
    conn.execute(
        "INSERT INTO memories (user_id, sender, content, ts, importance) VALUES ('10002','user','访客旧记忆',?,1.0)",
        (_iso(40),),
    )
    # 主人 40 天前高 importance 记忆 → 保留
    conn.execute(
        "INSERT INTO memories (user_id, sender, content, ts, importance) VALUES ('owner','user','主人重要旧记忆',?,2.0)",
        (_iso(40),),
    )
    conn.commit()
    conn.close()

    asyncio.run(evict_stale())

    conn = connect()
    rows = conn.execute("SELECT content FROM memories").fetchall()
    conn.close()
    contents = [r["content"] for r in rows]
    assert "访客旧记忆" not in contents
    assert "主人重要旧记忆" in contents


def test_top_topics_owner_only(db_env):
    from app.services.analyzer import top_topics

    conn = connect()
    now_iso = _iso(0)
    conn.execute(
        "INSERT INTO memories (user_id, sender, content, ts, topics) VALUES ('owner','user','主人话题','2026-08-20T00:00:00+00:00',?)",
        ('["主人专属话题"]',),
    )
    conn.execute(
        "INSERT INTO memories (user_id, sender, content, ts, topics) VALUES ('10002','user','访客话题',?,?)",
        (now_iso, '["访客专属话题"]'),
    )
    conn.commit()
    conn.close()

    topics = [t["topic"] for t in top_topics(days=7)]
    assert "访客专属话题" not in topics


def test_weekly_stats_counts_owner_only(db_env):
    from app.services.analyzer import weekly_stats

    conn = connect()
    now_iso = _iso(0)
    conn.execute(
        "INSERT INTO memories (user_id, sender, content, ts) VALUES ('owner','user','主人今日消息',?)",
        (now_iso,),
    )
    conn.execute(
        "INSERT INTO memories (user_id, sender, content, ts) VALUES ('10002','user','访客今日消息',?)",
        (now_iso,),
    )
    conn.commit()
    conn.close()

    stats = weekly_stats(days=7)
    assert stats["本周对话条数"] == 1  # 只数主人


def test_daily_summary_scope_sql_owner(db_env):
    """日报查询只读主人记忆（不跑 LLM，只验证 SQL 过滤路径通过）。"""
    from app.services.daily_summary import run_daily_summary

    conn = connect()
    now_iso = _iso(0)
    conn.execute(
        "INSERT INTO memories (user_id, sender, content, ts, summary) VALUES ('10002','user','访客有摘要','2026-08-20T00:00:00+00:00','访客摘要不应出现')"
    )
    conn.commit()
    conn.close()
    result = asyncio.run(run_daily_summary())
    # 访客摘要不构成主人活动 → 主人今天无数据 → 跳过（不烧 LLM）
    assert result.get("skipped") is True
