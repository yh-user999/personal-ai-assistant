"""情绪感知测试：第 6.23 课（零成本规则）+ 第 6.27 课（情绪记忆层 + 反馈闭环）。"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

from app.config import settings  # noqa: E402
from app.models.database import connect, init_db  # noqa: E402
from app.services import mood  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")


# ── 第 6.23 课：规则检测 ───────────────────────────────────

def test_tired():
    g = mood.detect_mood("今天累死了，先不聊了")
    assert "疲惫" in g
    assert "简短" in g


def test_urgent():
    g = mood.detect_mood("赶紧帮我看看这个报错，在线等")
    assert "着急" in g
    assert "直接给答案" in g


def test_annoyed():
    g = mood.detect_mood("烦死了，这个bug又出现了")
    assert "情绪不佳" in g
    assert "共情" in g


def test_low():
    g = mood.detect_mood("有点难过，感觉没什么意思")
    assert "低落" in g


def test_neutral_empty():
    assert mood.detect_mood("帮我看看F盘的目录") == ""


def test_detect_mood_name():
    assert mood.detect_mood_name("累死了") == "疲惫"
    assert mood.detect_mood_name("烦死") == "烦躁"
    assert mood.detect_mood_name("哈哈太爽了") == "开心"
    assert mood.detect_mood_name("随便聊聊") is None


# ── 第 6.27 课 A 档：情绪记忆层 ────────────────────────────

@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    init_db()
    yield


def _insert(mood_name, hours_ago=0):
    conn = connect()
    ts = (datetime.now(TZ) - timedelta(hours=hours_ago)).astimezone(ZoneInfo("UTC")).isoformat()
    conn.execute("INSERT INTO mood_log (mood, snippet, created_at) VALUES (?, '', ?)", (mood_name, ts))
    conn.commit()
    conn.close()


def test_record_mood(db):
    rid = mood.record_mood("烦躁", "烦死了，又报错")
    assert rid > 0
    conn = connect()
    row = conn.execute("SELECT mood, snippet FROM mood_log WHERE id=?", (rid,)).fetchone()
    conn.close()
    assert row["mood"] == "烦躁"
    assert row["snippet"] == "烦死了，又报错"


def test_today_injection_counts(db):
    _insert("烦躁")
    _insert("烦躁")
    _insert("疲惫")
    text = mood.get_today_injection()
    assert "烦躁×2" in text and "疲惫×1" in text
    assert "今日情绪" in text


def test_today_injection_excludes_yesterday(db):
    _insert("开心", hours_ago=25)  # 昨天的开心不算今天
    assert mood.get_today_injection() == ""


def test_today_injection_empty(db):
    assert mood.get_today_injection() == ""


# ── 第 6.27 课 B 档：反馈闭环 ──────────────────────────────

def test_streak_two_negative_triggers(db):
    _insert("烦躁")
    _insert("疲惫")
    text = mood.get_streak_injection()
    assert "倾听" in text and "连续 2 轮" in text


def test_streak_three(db):
    _insert("低落")
    _insert("烦躁")
    _insert("烦躁")
    assert "连续 3 轮" in mood.get_streak_injection()


def test_streak_broken_by_positive(db):
    _insert("开心")
    _insert("烦躁")
    assert mood.get_streak_injection() == ""


def test_streak_neutral_between(db):
    _insert("烦躁")
    _insert("着急")
    _insert("烦躁")
    assert mood.get_streak_injection() == ""  # 着急打断连击


def test_streak_single_negative_no_trigger(db):
    _insert("烦躁")
    assert mood.get_streak_injection() == ""


def test_state_injection_combines(db):
    _insert("烦躁")
    _insert("烦躁")
    text = mood.get_state_injection()
    assert "今日情绪" in text and "倾听" in text
