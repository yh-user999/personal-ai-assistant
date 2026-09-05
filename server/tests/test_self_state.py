"""自我状态测试：小月自己的处境（熟络度 / 久别 / 刚被纠正）。

覆盖点：今天轮数计数、久别提示阈值、刚被纠正的时效、访客不读 lessons
（单用户表，零跨用户泄漏）、无数据时的兜底文案。
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

from app.models.database import connect
from app.services.self_state import get_self_state_injection

TZ = ZoneInfo("Asia/Shanghai")


def _insert_message(sender: str, when: datetime, user_id: str = "owner") -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO memories (user_id, sender, content, ts) VALUES (?, ?, 'x', ?)",
        (user_id, sender, when.astimezone(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def _today(hour: int = 12) -> datetime:
    return datetime.now(TZ).replace(hour=hour, minute=0, second=0, microsecond=0)


def _insert_lesson(hours_ago: float) -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO lessons (content, context, created_at, kind) VALUES (?, '', ?, 'style')",
        (f"教训-{hours_ago}", (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()),
    )
    conn.commit()
    conn.close()


def test_first_message_of_day(db):
    text = get_self_state_injection()
    assert "今天还是第一句话" in text
    assert text.startswith("你自己的状态：")


def test_counts_today_turns(db):
    for _ in range(3):
        _insert_message("user", _today())
    _insert_message("assistant", _today())  # 助手侧不计入"聊了几轮"
    text = get_self_state_injection()
    assert "聊了 3 轮" in text


def test_warmth_tiers(db):
    for _ in range(8):
        _insert_message("user", _today())
    assert "聊开了" in get_self_state_injection()
    for _ in range(15):
        _insert_message("user", _today())
    assert "聊了很久" in get_self_state_injection()


def test_long_gap_noted(db):
    _insert_message("user", datetime.now(TZ) - timedelta(days=5))
    text = get_self_state_injection()
    assert "久别" in text and "5 天" in text


def test_short_gap_not_noted(db):
    _insert_message("user", datetime.now(TZ) - timedelta(days=1))
    assert "久别" not in get_self_state_injection()


def test_recent_correction_makes_her_careful(db):
    _insert_lesson(hours_ago=2)
    assert "刚被纠正" in get_self_state_injection()


def test_old_correction_not_noted(db):
    _insert_lesson(hours_ago=48)
    assert "刚被纠正" not in get_self_state_injection()


def test_guest_never_reads_lessons(db):
    """lessons 是主人单用户表：访客路径不得读到它。"""
    _insert_lesson(hours_ago=1)
    assert "刚被纠正" not in get_self_state_injection(user_id="123456")


def test_guest_turns_isolated(db):
    """轮数按用户隔离：主人的消息不算进访客的熟络度。"""
    for _ in range(5):
        _insert_message("user", _today(), user_id="owner")
    text = get_self_state_injection(user_id="123456")
    assert "今天还是第一句话" in text
