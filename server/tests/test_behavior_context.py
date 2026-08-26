"""行为上下文注入测试：新鲜度/时区/空值/聚合。"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")
os.environ.setdefault("DB_PATH", "/tmp/test_behavior_ctx.db")

from app.models.database import connect, init_db  # noqa: E402
from app.services.behavior_context import (  # noqa: E402
    get_behavior_injection,
    get_current_window,
    get_recent_activity,
    get_today_commits,
)

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime.now(TZ)


def _seed(kind, name, detail="", start=None, end=None):
    conn = connect()
    conn.execute(
        """INSERT INTO behavior_events (kind, name, detail, start_ts, end_ts)
           VALUES (?, ?, ?, ?, ?)""",
        (kind, name, detail, start or NOW.isoformat(), end),
    )
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def fresh_db():
    init_db()
    # 清空共享库的行为事件（settings 单例导致多测试文件共享一库）
    conn = connect()
    conn.execute("DELETE FROM behavior_events")
    conn.commit()
    conn.close()
    yield


def test_empty_db_returns_empty():
    assert get_behavior_injection() == ""


def test_fresh_window_injected():
    _seed("app_usage", "vscode", "main.py - 项目", NOW.isoformat())
    assert "vscode" in get_current_window()


def test_stale_window_skipped():
    """过时窗口（>10 分钟且已结束）不注入——防误导。"""
    stale = NOW - timedelta(minutes=30)
    _seed("app_usage", "chrome", "旧窗口", stale.isoformat(),
          (stale + timedelta(minutes=5)).isoformat())
    assert get_current_window() is None


def test_long_staying_window_still_fresh():
    """无 end_ts（还在这个窗口）且停留 <60 分钟 → 仍视为当前窗口。"""
    stay = NOW - timedelta(minutes=25)
    _seed("app_usage", "vscode", "main.py", stay.isoformat())  # 无 end_ts
    assert "vscode" in (get_current_window() or "")


def test_today_commits():
    _seed("git_commit", "repo", "fix: 测试", NOW.isoformat())
    text = get_today_commits()
    assert text and "1 次" in text


def test_yesterday_commits_not_counted():
    yesterday = (NOW - timedelta(days=1)).isoformat()
    _seed("git_commit", "repo", "昨天提交", yesterday)
    assert get_today_commits() is None  # "今天"按北京时间，昨天的不算


def test_recent_activity_aggregation():
    start = (NOW - timedelta(minutes=30)).isoformat()
    end = NOW.isoformat()
    _seed("app_usage", "vscode", "", start, end)
    text = get_recent_activity(hours=1)
    assert text and "vscode" in text


def test_behavior_injection_combines():
    _seed("app_usage", "vscode", "main.py", NOW.isoformat())
    _seed("git_commit", "repo", "fix", NOW.isoformat())
    text = get_behavior_injection()
    assert "vscode" in text and "git" in text
