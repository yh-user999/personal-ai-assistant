"""第 8 课测试：QQ 提醒推送 + 电脑在线状态提示。"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")
os.environ.setdefault("DB_PATH", "/tmp/test_qq_push.db")

from app.config import settings  # noqa: E402
from app.models.database import connect, init_db  # noqa: E402
from app.services import qq_push, reminders  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    init_db()
    # 清掉 QQ 推送配置（各用例自行设置）
    monkeypatch.setattr(settings, "qq_push_url", "")
    monkeypatch.setattr(settings, "qq_push_token", "")
    monkeypatch.setattr(settings, "qq_admin_id", "")
    yield


def _seed_due_reminder():
    conn = connect()
    # 与 reminders._utc_str 同格式（无微秒无时区标记），字符串比较才成立
    past = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        "INSERT INTO reminders (content, remind_at, status, created_at) "
        "VALUES ('喝水', ?, 'pending', ?)",
        (past, past),
    )
    conn.commit()
    conn.close()


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.text = ""
        self._payload = payload or {"status": "ok"}

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResp()


def test_push_disabled_without_config():
    _seed_due_reminder()
    import asyncio

    assert asyncio.run(qq_push.push_reminders()) == 0


def test_push_sends_and_consumes(monkeypatch):
    import asyncio

    monkeypatch.setattr(settings, "qq_push_url", "http://127.0.0.1:3100")
    monkeypatch.setattr(settings, "qq_push_token", "t-token")
    monkeypatch.setattr(settings, "qq_admin_id", "10001")
    fake = _FakeAsyncClient()
    monkeypatch.setattr(qq_push.httpx, "AsyncClient", lambda **kw: fake)
    _seed_due_reminder()

    pushed = asyncio.run(qq_push.push_reminders())
    assert pushed == 1
    assert len(fake.calls) == 1
    assert fake.calls[0]["url"] == "http://127.0.0.1:3100/send_private_msg"
    assert fake.calls[0]["json"]["user_id"] == 10001
    assert "喝水" in fake.calls[0]["json"]["message"]
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer t-token"
    # 取即消费：第二次推送为空
    assert asyncio.run(qq_push.push_reminders()) == 0


def test_push_failure_counted_zero(monkeypatch):
    import asyncio

    monkeypatch.setattr(settings, "qq_push_url", "http://127.0.0.1:3100")
    monkeypatch.setattr(settings, "qq_admin_id", "123456")
    _seed_due_reminder()

    class _BadClient(_FakeAsyncClient):
        async def post(self, url, json=None, headers=None):
            return _FakeResp(status_code=500, payload={"status": "failed"})

    monkeypatch.setattr(qq_push.httpx, "AsyncClient", lambda **kw: _BadClient())
    assert asyncio.run(qq_push.push_reminders()) == 0


# ── 电脑在线提示（chat 执行器分支）────────────────────────

def test_computer_online_helper():
    from app.api.chat import _computer_online

    assert _computer_online(None) is False
    assert _computer_online({}) is False
    fresh = datetime.now(timezone.utc).isoformat()
    assert _computer_online({"received_at": fresh}) is True
    stale = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
    assert _computer_online({"received_at": stale}) is False


def test_executor_offline_note(monkeypatch):
    """电脑心跳为空时，执行器指令回复带'电脑不在线'提示。"""
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, "executor_allowed_roots", "F:/")
    with TestClient(app) as client:
        app.state.collector_heartbeat = None  # lifespan 启动后设置才不被重置
        r = client.post(
            "/api/chat",
            json={"message": "帮我打开F:/test"},
            headers={"Authorization": "Bearer test-secret-token"}
            if settings.api_token
            else {},
        )
        assert r.status_code == 200
        assert "已收到指令" in r.json()["reply"]
        assert "电脑当前不在线" in r.json()["reply"]


def test_executor_online_no_note(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, "executor_allowed_roots", "F:/")
    with TestClient(app) as client:
        app.state.collector_heartbeat = {
            "received_at": datetime.now(timezone.utc).isoformat()
        }
        r = client.post(
            "/api/chat",
            json={"message": "帮我打开F:/test"},
            headers={"Authorization": "Bearer test-secret-token"}
            if settings.api_token
            else {},
        )
        assert r.status_code == 200
        assert "电脑当前不在线" not in r.json()["reply"]
