"""主动开口测试：重点是"不打扰"的四条硬约束能被验证。

覆盖点：默认关闭、夜间静默、每日 1 条上限、连续无回应自动降频、
同一搁置话题不问第二次、推送失败不入账（下轮可重试）。
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

from app.config import settings
from app.models.database import connect
from app.services import initiative

TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def channel(db, monkeypatch):
    """启用主动开口 + 配好 QQ 通道 + 非静默时刻（各用例再单点覆盖）。"""
    monkeypatch.setattr(settings, "initiative_enabled", True)
    monkeypatch.setattr(settings, "qq_push_url", "http://127.0.0.1:3100")
    monkeypatch.setattr(settings, "qq_push_token", "t")
    monkeypatch.setattr(settings, "qq_admin_id", "10001")
    monkeypatch.setattr(initiative, "in_quiet_hours", lambda *a, **k: False)
    yield


def _seed_summary(content: str = "**今天调了 3 小时 RAG**\n- 重排序调参") -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO daily_summaries (date, content, created_at) VALUES (?, ?, ?)",
        (datetime.now(TZ).date().isoformat(), content, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def _seed_concern(topic: str, days_ago: int = 10, count: int = 3) -> None:
    from app.core.memory import owner_user_id

    conn = connect()
    conn.execute(
        "INSERT INTO concerns (user_id, topic, mention_count, last_mentioned_at) "
        "VALUES (?, ?, ?, ?)",
        (owner_user_id(), topic, count,
         (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()),
    )
    conn.commit()
    conn.close()


class _Sent:
    """记录推送内容的假通道。"""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.messages: list[str] = []

    async def __call__(self, text: str) -> bool:
        self.messages.append(text)
        return self.ok


def _fake_line(text: str = "今天你在 RAG 上花了 3 小时，进展看着不错。早点睡？"):
    async def _build(_summary: str) -> str:
        return text
    return _build


# ── 硬约束 ────────────────────────────────────────────────

def test_disabled_by_default(db):
    """默认关闭：不配 .env 开关就绝不主动开口。"""
    assert settings.initiative_enabled is False
    _seed_summary()
    result = asyncio.run(initiative.run_initiative())
    assert result["skipped"] is True
    assert "未启用" in result["reason"]


def test_requires_qq_channel(db, monkeypatch):
    monkeypatch.setattr(settings, "initiative_enabled", True)
    monkeypatch.setattr(settings, "qq_push_url", "")
    ok, reason = initiative.should_speak()
    assert ok is False and "QQ 通道" in reason


def test_quiet_hours_block(channel, monkeypatch):
    """夜间静默：默认 23:00-08:00 不推。"""
    monkeypatch.setattr(initiative, "in_quiet_hours", lambda *a, **k: True)
    _seed_summary()
    result = asyncio.run(initiative.run_initiative())
    assert result["skipped"] is True and "静默" in result["reason"]


def test_quiet_hours_boundaries(db, monkeypatch):
    monkeypatch.setattr(settings, "initiative_quiet_start", 23)
    monkeypatch.setattr(settings, "initiative_quiet_end", 8)
    at = lambda h: datetime.now(TZ).replace(hour=h)
    assert initiative.in_quiet_hours(at(23)) is True
    assert initiative.in_quiet_hours(at(3)) is True
    assert initiative.in_quiet_hours(at(7)) is True
    assert initiative.in_quiet_hours(at(8)) is False
    assert initiative.in_quiet_hours(at(22)) is False


def test_quiet_hours_disabled_when_equal(db, monkeypatch):
    """start == end 视为不静默（配错不至于让通道整体失效）。"""
    monkeypatch.setattr(settings, "initiative_quiet_start", 8)
    monkeypatch.setattr(settings, "initiative_quiet_end", 8)
    assert initiative.in_quiet_hours(datetime.now(TZ).replace(hour=3)) is False


def test_daily_limit_one(channel, monkeypatch):
    """每日最多 1 条。"""
    sent = _Sent()
    monkeypatch.setattr(initiative, "_push", sent)
    monkeypatch.setattr(initiative, "build_daily_line", _fake_line())
    _seed_summary()

    first = asyncio.run(initiative.run_initiative())
    assert first["sent"] == "daily"
    second = asyncio.run(initiative.run_initiative())
    assert second["skipped"] is True and "上限" in second["reason"]
    assert len(sent.messages) == 1


def test_ignored_triggers_cooldown(channel):
    """连续 3 条无回应 → 降频（一周最多 1 条）。"""
    now = datetime.now(timezone.utc)
    conn = connect()
    for i in range(3):
        conn.execute(
            "INSERT INTO initiative_log (kind, content, sent_at, responded) "
            "VALUES ('daily', 'x', ?, 0)",
            ((now - timedelta(days=i + 1)).isoformat(),),
        )
    conn.commit()
    conn.close()
    assert initiative.is_ignored() is True
    ok, reason = initiative.should_speak()
    assert ok is False and "降频" in reason


def test_response_resets_streak(channel):
    now = datetime.now(timezone.utc)
    conn = connect()
    for i, responded in enumerate((0, 1, 0)):
        conn.execute(
            "INSERT INTO initiative_log (kind, content, sent_at, responded) "
            "VALUES ('daily', 'x', ?, ?)",
            ((now - timedelta(days=i + 2)).isoformat(), responded),
        )
    conn.commit()
    conn.close()
    assert initiative.is_ignored() is False


def test_mark_responded_marks_latest(channel):
    initiative.log_sent("daily", "今天怎么样")
    assert initiative.mark_responded() == 1
    conn = connect()
    responded = conn.execute(
        "SELECT responded FROM initiative_log ORDER BY id DESC LIMIT 1"
    ).fetchone()["responded"]
    conn.close()
    assert responded == 1
    assert initiative.mark_responded() == 0  # 没有未回应项了


def test_cooldown_expires_after_a_week(channel):
    """降频不是永久封口：超过冷却期还能再开口一次。"""
    old = datetime.now(timezone.utc) - timedelta(days=10)
    conn = connect()
    for i in range(3):
        conn.execute(
            "INSERT INTO initiative_log (kind, content, sent_at, responded) "
            "VALUES ('daily', 'x', ?, 0)",
            ((old - timedelta(days=i)).isoformat(),),
        )
    conn.commit()
    conn.close()
    ok, reason = initiative.should_speak()
    assert ok is True, reason


# ── 内容与可靠性 ──────────────────────────────────────────

def test_push_failure_not_logged(channel, monkeypatch):
    """推送失败不入账——否则会占掉当天配额且永不重试。"""
    monkeypatch.setattr(initiative, "_push", _Sent(ok=False))
    monkeypatch.setattr(initiative, "build_daily_line", _fake_line())
    _seed_summary()
    result = asyncio.run(initiative.run_initiative())
    assert result["skipped"] is True
    conn = connect()
    n = conn.execute("SELECT COUNT(*) AS c FROM initiative_log").fetchone()["c"]
    conn.close()
    assert n == 0


def test_falls_back_to_stale_concern(channel, monkeypatch):
    """没有小结时，退回搁置话题续上。"""
    sent = _Sent()
    monkeypatch.setattr(initiative, "_push", sent)
    _seed_concern("RAG 调参")
    result = asyncio.run(initiative.run_initiative())
    assert result["sent"] == "concern"
    assert "RAG 调参" in sent.messages[0]


def test_same_concern_never_asked_twice(channel, monkeypatch):
    """同一话题只问一次（问两遍就从关心变催促）。"""
    monkeypatch.setattr(initiative, "_push", _Sent())
    _seed_concern("RAG 调参")
    assert asyncio.run(initiative.run_initiative())["sent"] == "concern"

    # 清掉当日上限与 concern 间隔的影响，只留"问过"这一条约束
    conn = connect()
    conn.execute("DELETE FROM initiative_log")
    conn.commit()
    conn.close()
    assert initiative.pick_stale_concern() is None


def test_concern_min_interval(channel, monkeypatch):
    """关切类追问彼此至少隔一周，不连着问不同话题。"""
    _seed_concern("RAG 调参")
    _seed_concern("健身计划")
    initiative.log_sent("concern", "上次说的 X 怎么样了", topic="X")
    assert initiative.pick_stale_concern() is None


def test_no_content_no_push(channel, monkeypatch):
    """既无小结也无搁置话题 → 什么都不说（不硬凑一句寒暄）。"""
    sent = _Sent()
    monkeypatch.setattr(initiative, "_push", sent)
    result = asyncio.run(initiative.run_initiative())
    assert result["skipped"] is True
    assert sent.messages == []


def test_empty_llm_line_falls_through(channel, monkeypatch):
    """LLM 没给出可用文案时不推空消息。"""
    sent = _Sent()
    monkeypatch.setattr(initiative, "_push", sent)
    monkeypatch.setattr(initiative, "build_daily_line", _fake_line(""))
    _seed_summary()
    result = asyncio.run(initiative.run_initiative())
    assert result["skipped"] is True
    assert sent.messages == []


def test_build_daily_line_empty_summary(db):
    assert asyncio.run(initiative.build_daily_line("  ")) == ""


def test_build_daily_line_llm_failure(db, monkeypatch):
    from app.core import llm

    async def _boom(*a, **k):
        raise RuntimeError("LLM 挂了")

    monkeypatch.setattr(llm, "chat_json", _boom)
    assert asyncio.run(initiative.build_daily_line("今天做了点事")) == ""
