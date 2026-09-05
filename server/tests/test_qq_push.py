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

from app.config import settings
from app.models.database import connect, init_db
from app.services import qq_push


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
    is_closed = False  # 对齐 httpx.AsyncClient 接口（_get_client 检查用）

    def __init__(self, *args, **kwargs):
        self.calls = []

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResp()


def _use_client(monkeypatch, client):
    """新实现用模块级长驻客户端，测试直接注入实例。"""
    monkeypatch.setattr(qq_push, "_client", client)


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
    _use_client(monkeypatch, fake)
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


def test_send_private_is_single_exit(monkeypatch):
    """所有 QQ 推送走这一个出口。

    收敢动因：原先三处各自实现（提醒推送 / 主动开口 / 任务失败告警），
    后两处每次调用都新建 httpx.AsyncClient——连接不复用，且"成功"判据散在
    三个文件里（LESSONS 第 16 条：同一判据出现在多处等于都不可信）。
    """
    import asyncio

    monkeypatch.setattr(settings, "qq_push_url", "http://127.0.0.1:3100")
    monkeypatch.setattr(settings, "qq_push_token", "t-token")
    monkeypatch.setattr(settings, "qq_admin_id", "10001")
    fake = _FakeAsyncClient()
    _use_client(monkeypatch, fake)

    assert asyncio.run(qq_push.send_private("测试消息")) is True
    assert len(fake.calls) == 1
    assert fake.calls[0]["json"]["message"] == "测试消息"
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer t-token"


def test_send_private_without_config(monkeypatch):
    import asyncio

    monkeypatch.setattr(settings, "qq_push_url", "")
    assert asyncio.run(qq_push.send_private("x")) is False


def test_send_private_reuses_client(monkeypatch):
    """复用长驻客户端，不逐次新建连接。"""
    import asyncio

    monkeypatch.setattr(settings, "qq_push_url", "http://127.0.0.1:3100")
    monkeypatch.setattr(settings, "qq_admin_id", "10001")
    fake = _FakeAsyncClient()
    _use_client(monkeypatch, fake)
    for _ in range(3):
        asyncio.run(qq_push.send_private("x"))
    assert len(fake.calls) == 3, "三次调用应复用同一 client"


def test_initiative_uses_shared_exit(monkeypatch):
    """主动开口不再自建 client——它应该调 qq_push.send_private。"""
    import asyncio

    from app.services import initiative

    monkeypatch.setattr(settings, "qq_push_url", "http://127.0.0.1:3100")
    monkeypatch.setattr(settings, "qq_admin_id", "10001")
    fake = _FakeAsyncClient()
    _use_client(monkeypatch, fake)
    assert asyncio.run(initiative._push("主动消息")) is True
    assert fake.calls and fake.calls[0]["json"]["message"] == "主动消息"


def test_push_failure_counted_zero(monkeypatch):
    import asyncio

    monkeypatch.setattr(settings, "qq_push_url", "http://127.0.0.1:3100")
    monkeypatch.setattr(settings, "qq_admin_id", "123456")
    _seed_due_reminder()

    class _BadClient(_FakeAsyncClient):
        async def post(self, url, json=None, headers=None):
            return _FakeResp(status_code=500, payload={"status": "failed"})

    _use_client(monkeypatch, _BadClient())
    assert asyncio.run(qq_push.push_reminders()) == 0
    # 失败不消费：提醒留在 pending，下一分钟重推（防静默丢失的核心语义）
    conn = connect()
    status = conn.execute("SELECT status FROM reminders WHERE content='喝水'").fetchone()["status"]
    conn.close()
    assert status == "pending"


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
