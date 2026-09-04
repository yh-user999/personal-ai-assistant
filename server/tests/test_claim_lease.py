"""持久化 claim/lease 回归测试。"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.database import connect, init_db
from app.services import executor, reminders


def _insert_due_reminder():
    conn = connect()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    cur = conn.execute(
        "INSERT INTO reminders(content, remind_at, status, created_at) VALUES (?, ?, 'pending', ?)",
        ("claim-test", now, now),
    )
    conn.commit()
    return cur.lastrowid


def test_executor_claim_token_and_late_result(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "claim.db"))
    init_db()
    cmd_id = executor.enqueue("open", "Chrome")
    claimed = executor.get_pending("device-a")
    assert claimed["id"] == cmd_id
    assert claimed["claim_token"]
    assert not executor.mark_result(cmd_id, True, "bad", "wrong-token", "device-a")
    assert executor.mark_result(cmd_id, True, "ok", claimed["claim_token"], "device-a")


def test_reminder_sending_claim_is_exclusive(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "reminder.db"))
    init_db()
    reminder_id = _insert_due_reminder()
    first = reminders.due_reminders()
    second = reminders.due_reminders()
    assert [item["id"] for item in first] == [reminder_id]
    assert second == []
    reminders.mark_notified([reminder_id], first[0]["sending_token"])
    assert reminders.due_reminders() == []
