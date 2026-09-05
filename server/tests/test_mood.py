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

from app.config import settings
from app.models.database import connect, init_db
from app.services import mood

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


def test_streak_expires_after_two_hours(db):
    """昨天的烦躁不该让今天一整天都在倾听模式里（时效护栏）。"""
    _insert("烦躁", hours_ago=3)
    _insert("烦躁", hours_ago=4)
    assert mood.get_streak_injection() == ""


def test_state_injection_combines(db):
    _insert("烦躁")
    _insert("烦躁")
    text = mood.get_state_injection()
    assert "今日情绪" in text and "倾听" in text


# ── 隔日跟进：真人会"记得昨天" ──────────────────────────────

def _insert_yesterday(mood_name: str, hour: int = 12):
    """按本地日期精确插到昨天（hours_ago 在跨零点时会算错日子）。"""
    day = (datetime.now(TZ) - timedelta(days=1)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    conn = connect()
    conn.execute(
        "INSERT INTO mood_log (mood, snippet, created_at) VALUES (?, '', ?)",
        (mood_name, day.astimezone(ZoneInfo("UTC")).isoformat()),
    )
    conn.commit()
    conn.close()


def test_yesterday_followup_triggers(db):
    _insert_yesterday("烦躁", hour=10)
    _insert_yesterday("烦躁", hour=15)
    text = mood.get_yesterday_followup()
    assert "昨天" in text and "烦躁×2" in text
    assert "问过就别再提" in text


def test_yesterday_followup_needs_two_negatives(db):
    _insert_yesterday("烦躁", hour=10)
    assert mood.get_yesterday_followup() == ""


def test_yesterday_followup_ignores_positive(db):
    _insert_yesterday("开心", hour=10)
    _insert_yesterday("开心", hour=15)
    assert mood.get_yesterday_followup() == ""


def test_yesterday_followup_skipped_after_first_message_today(db):
    """今天已经聊过 → 跟进时机已过，不在对话中途回头问昨天。"""
    _insert_yesterday("烦躁", hour=10)
    _insert_yesterday("疲惫", hour=15)
    _insert("着急")  # 今天的记录
    assert mood.get_yesterday_followup() == ""


def test_yesterday_followup_empty_db(db):
    assert mood.get_yesterday_followup() == ""


def test_state_injection_includes_yesterday_followup(db):
    _insert_yesterday("烦躁", hour=10)
    _insert_yesterday("低落", hour=16)
    assert "昨天" in mood.get_state_injection()


# ── 第 6.28 课 C1：情绪周报统计 ────────────────────────────

def test_weekly_stats_counts(db):
    _insert("烦躁")
    _insert("烦躁")
    _insert("开心")
    s = mood.get_weekly_stats(7)
    assert s["total"] == 3
    assert s["by_mood"] == {"烦躁": 2, "开心": 1}
    assert s["peak_hours"]  # 非空（3 小时桶）


def test_weekly_stats_negative_topics(db):
    conn = connect()
    conn.executemany(
        "INSERT INTO mood_log (mood, snippet, created_at) VALUES (?, ?, ?)",
        [
            ("烦躁", "烦死了，bug又报错", mood._utc_now()),
            ("烦躁", "烦死了，bug又报错", mood._utc_now()),
            ("疲惫", "累死了，改了一天", mood._utc_now()),
        ],
    )
    conn.commit()
    conn.close()
    s = mood.get_weekly_stats(7)
    assert s["negative_topics"] == ["烦死了，bug又报错", "累死了，改了一天"]


def test_weekly_stats_excludes_old(db):
    _insert("开心", hours_ago=8 * 24)  # 8 天前不计入
    s = mood.get_weekly_stats(7)
    assert s["total"] == 0
    assert s["by_mood"] == {}


def test_weekly_report_section(db):
    conn = connect()
    conn.execute(
        "INSERT INTO mood_log (mood, snippet, created_at) VALUES ('烦躁', '烦死了，bug又报错', ?)",
        (mood._utc_now(),),
    )
    conn.commit()
    conn.close()
    text = mood.weekly_report_section(7)
    assert text.startswith("## 本周情绪")
    assert "烦躁×1" in text
    assert "烦死了，bug又报错" in text
    assert "情绪高峰" in text


def test_weekly_report_section_empty(db):
    assert mood.weekly_report_section(7) == ""
