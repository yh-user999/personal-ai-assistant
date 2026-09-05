"""个性化问候服务测试：时段/日期/行为数据组装。"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

from app.config import settings
from app.models.database import init_db, reset_connections
from app.services import greeting


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """独立临时库（原 DELETE FROM behavior_events 实际跑在生产库上）。"""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    reset_connections()
    init_db()
    yield
    reset_connections()


def test_greeting_contains_date():
    text = greeting.get_greeting()
    assert "今天是" in text
    # 含周几
    assert any(w in text for w in greeting._WEEKDAYS)


def test_greeting_variety():
    """随机池：多次生成应出现不同问候（概率性，取 20 次应有变化）。"""
    samples = {greeting.get_greeting() for _ in range(20)}
    assert len(samples) > 1


def test_greeting_hour_buckets(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    class FakeNow:
        def __init__(self, hour):
            self._h = hour

        def __call__(self, tz=None):
            return datetime(2026, 8, 26, self._h, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

    import app.services.greeting as g
    # 档位：<5 深夜(凌晨/夜深) / 5-9 早上 / 9-12 中午 / 12-18 下午 / 18-23 晚上
    for h, kw in [(7, "早"), (11, "中午"), (15, "下午"), (21, "晚上"),
                  (2, ("凌晨", "夜深"))]:
        monkeypatch.setattr(g, "datetime", type("D", (), {"now": FakeNow(h)}))
        text = g.get_greeting()
        ok = any(k in text for k in kw) if isinstance(kw, tuple) else kw in text
        assert ok, f"hour={h} 应含 {kw}"
