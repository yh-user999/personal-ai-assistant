"""第 6.24 课测试：定时提醒——中文时间命令解析 + CRUD + 到期推送。"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.database import connect, init_db  # noqa: E402
from app.config import settings  # noqa: E402
from app.services import reminders  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def db(monkeypatch, tmp_path):
    """临时库 + 建表，测完丢弃。"""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    init_db()
    yield
    # 清掉模块级连接状态，避免影响其他用例
    reminders.__dict__.pop("_conn", None)


# ── 时间解析 ───────────────────────────────────────────────

def test_parse_relative_minutes():
    content, dt = reminders.parse_reminder_cmd("30分钟后提醒我喝水")
    assert content == "喝水"
    expected = datetime.now(TZ) + timedelta(minutes=30)
    assert abs((dt - expected).total_seconds()) < 5


def test_parse_relative_hours():
    content, dt = reminders.parse_reminder_cmd("2小时后提醒我交周报")
    assert content == "交周报"
    assert dt.hour == (datetime.now(TZ) + timedelta(hours=2)).hour


def test_parse_mingzao():
    content, dt = reminders.parse_reminder_cmd("明早9点提醒我开会")
    assert content == "开会"
    tomorrow = datetime.now(TZ).date() + timedelta(days=1)
    assert dt.date() == tomorrow and dt.hour == 9 and dt.minute == 0


def test_parse_mingwan_evening_hour():
    """明晚10点 = 22:00（不是上午10点）——回归：明晚前缀丢失 bug。"""
    content, dt = reminders.parse_reminder_cmd("明晚10点提醒我睡觉")
    assert content == "睡觉"
    assert dt.hour == 22


def test_parse_today_pm():
    content, dt = reminders.parse_reminder_cmd("今晚8点提醒我看球")
    assert content == "看球"
    assert dt.hour == 20


def test_parse_afternoon():
    content, dt = reminders.parse_reminder_cmd("下午3点提醒我打电话")
    assert content == "打电话"
    assert dt.hour == 15


def test_parse_tomorrow_no_time_defaults_9am():
    content, dt = reminders.parse_reminder_cmd("明天提醒我取快递")
    assert content == "取快递"
    assert dt.hour == 9


def test_parse_not_a_command():
    assert reminders.parse_reminder_cmd("今天天气怎么样") is None
    assert reminders.parse_reminder_cmd("记得提醒我哦") is None


# ── CRUD ───────────────────────────────────────────────────

def test_add_and_list(db):
    content, dt = reminders.parse_reminder_cmd("明早9点提醒我开会")
    rid = reminders.add_reminder(content, dt)
    assert rid > 0
    items = reminders.list_pending()
    assert any(i["content"] == "开会" for i in items)


def test_cancel_by_keyword(db):
    reminders.add_reminder("喝水", datetime.now(TZ) + timedelta(hours=1))
    reminders.add_reminder("开会", datetime.now(TZ) + timedelta(hours=2))
    assert reminders.cancel_by_keyword("开会") == 1
    items = reminders.list_pending()
    assert [i["content"] for i in items] == ["喝水"]
    assert reminders.cancel_by_keyword("不存在的提醒") == 0


def test_due_marks_notified_once(db):
    """到期项取出时不消费（推送成功后 mark_notified 才消费）——
    防止 NapCat 掉线期间提醒被静默吞掉。"""
    conn = connect()
    past = (datetime.now(TZ) - timedelta(minutes=1)).astimezone(ZoneInfo("UTC"))
    conn.execute(
        "INSERT INTO reminders (content, remind_at, status, created_at) "
        "VALUES ('已到期', ?, 'pending', ?)",
        (past.strftime("%Y-%m-%dT%H:%M:%S"), past.strftime("%Y-%m-%dT%H:%M:%S")),
    )
    conn.commit()
    conn.close()
    first = reminders.due_reminders()
    assert [i["content"] for i in first] == ["已到期"]
    assert first[0].get("sending_token")
    # 已被实例领取后，其他实例不可重复领取；推送失败释放后可重试。
    assert reminders.due_reminders() == []
    reminders.release_claim([first[0]["id"]], first[0]["sending_token"])
    retry = reminders.due_reminders()
    assert [i["content"] for i in retry] == ["已到期"]
    # 推送成功后显式消费 → 消失
    reminders.mark_notified([retry[0]["id"]], retry[0]["sending_token"])
    assert reminders.due_reminders() == []
    assert reminders.list_pending() == []


def test_list_shows_beijing_time(db):
    """回归：UTC 入库的字符串展示时必须先挂 UTC 再转东八区。
    （曾把 naive 时间当服务器本地时间，09:00 显示成 01:00）"""
    reminders.add_reminder("开会", datetime(2026, 8, 30, 9, 0, tzinfo=TZ))
    items = reminders.list_pending()
    assert items[0]["remind_at"] == "08月30日 09:00"
