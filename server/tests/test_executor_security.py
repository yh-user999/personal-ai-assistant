"""执行器安全加固测试（v0.2.1 整修）。

覆盖：白名单兄弟目录绕过、../ 穿越、open 脚本黑名单、
入队 API 强制校验、指令原子认领与失联释放。
sys.path 与测试环境由 tests/conftest.py 统一注入。
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")
os.environ.setdefault("DB_PATH", "/tmp/test_executor_security.db")

from common.file_ops import path_allowed

from app.config import settings
from app.models.database import connect, init_db
from app.services import executor as server_executor


@pytest.fixture(autouse=True)
def fresh_db():
    init_db()
    conn = connect()
    conn.execute("DELETE FROM executor_commands")
    conn.commit()
    conn.close()
    yield


# ── 白名单归一化（兄弟前缀 / 穿越）────────────────────────

def test_check_roots_sibling_prefix_denied(monkeypatch):
    """C:/Users/wfy33-evil 不属于根 C:/Users/wfy33（旧 startswith 实现会放行）。"""
    monkeypatch.setattr(settings, "executor_allowed_roots", "C:/Users/wfy33")
    assert server_executor.check_roots("C:/Users/wfy33/Desktop") is True
    assert server_executor.check_roots("C:/Users/wfy33-evil/evil.bat") is False


def test_check_roots_traversal_denied(monkeypatch):
    """.. 折叠后逃出白名单的路径拒绝。"""
    monkeypatch.setattr(settings, "executor_allowed_roots", "C:/Users/wfy33;F:/")
    assert server_executor.check_roots("C:/Users/wfy33/../../Windows/System32/x.bat") is False
    assert server_executor.check_roots("F:/a/../b.txt") is True  # 未逃出白名单的正常相对写法


def test_check_roots_root_itself_allowed(monkeypatch):
    monkeypatch.setattr(settings, "executor_allowed_roots", "F:/")
    assert server_executor.check_roots("F:/") is True
    assert server_executor.check_roots("F:/任何子路径") is True


def test_path_allowed_shared_env_value():
    """common 共享实现（collector/desktop 用的就是它）：显式传入 env 值。"""
    assert path_allowed("F:/data/x.txt", env_value="F:/data") is True
    assert path_allowed("F:/data-evil/x.bat", env_value="F:/data") is False  # 兄弟目录
    assert path_allowed("F:/data/x", env_value="") is False  # 未配置=全禁止


# ── open 脚本扩展名黑名单 ─────────────────────────────────

def test_open_blocked_ext_desktop():
    """桌面本地执行器：open 脚本类型直接拒绝（不触达 startfile）。"""
    from desktop import local_exec

    ok, text = local_exec._execute("open", "F:/anywhere/evil.bat")
    assert not ok
    assert "不允许打开" in text
    ok, _ = local_exec._execute("open", "F:/anywhere/x.ps1")
    assert not ok


def test_open_blocked_ext_collector():
    """采集器执行器：同样的黑名单。"""
    from collector.executor import Executor

    ok, text = Executor("http://x", "")._execute("open", "C:/tmp/evil.cmd")
    assert not ok
    assert "不允许打开" in text


# ── 入队 API 强制校验 ─────────────────────────────────────

@pytest.fixture
def api_client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "api_token", "")  # 测试关闭鉴权
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    from fastapi.testclient import TestClient

    from app.main import app

    init_db()
    return TestClient(app)


def test_enqueue_rejects_unknown_action(api_client):
    r = api_client.post("/api/executor/enqueue", json={"action": "format_disk", "target": "C:/"})
    assert r.status_code == 400


def test_enqueue_rejects_run_script_remotely(api_client):
    """run_script 属安全分级③，远程通道永不允许。"""
    r = api_client.post(
        "/api/executor/enqueue", json={"action": "run_script", "target": "F:/x.bat"}
    )
    assert r.status_code == 400


def test_enqueue_rejects_outside_whitelist(api_client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "executor_allowed_roots", str(tmp_path).replace("\\", "/"))
    r = api_client.post(
        "/api/executor/enqueue",
        json={"action": "read_file", "target": "C:/Users/wfy33-evil/secret.txt"},
    )
    assert r.status_code == 400


def test_enqueue_accepts_whitelisted(api_client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "executor_allowed_roots", str(tmp_path).replace("\\", "/"))
    r = api_client.post(
        "/api/executor/enqueue",
        json={"action": "read_file", "target": f"{tmp_path}/a.txt".replace("\\", "/")},
    )
    assert r.status_code == 200
    assert "id" in r.json()


# ── 原子认领与失联释放 ────────────────────────────────────

def test_claim_is_atomic_and_not_requeued():
    """认领后指令不再出现在 pending 队列（旧实现会重复返回同一条）。"""
    cmd_id = server_executor.enqueue("list_dir", "F:/")
    first = server_executor.get_pending()
    assert first["id"] == cmd_id
    assert server_executor.get_pending() is None  # 已认领，不会再次下发


def test_claimed_timeout_released():
    """执行器认领后失联：超时释放为 failed，不永久占队。"""
    cmd_id = server_executor.enqueue("list_dir", "F:/")
    assert server_executor.get_pending()["id"] == cmd_id
    stale = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
    conn = connect()
    conn.execute("UPDATE executor_commands SET claimed_at=? WHERE id=?", (stale, cmd_id))
    conn.commit()
    conn.close()

    assert server_executor.get_pending() is None  # 被释放为 failed，无 pending 可领
    conn = connect()
    row = conn.execute(
        "SELECT status, result FROM executor_commands WHERE id=?", (cmd_id,)
    ).fetchone()
    conn.close()
    assert row["status"] == "failed"
    assert "超时" in row["result"]


def test_pending_stale_expired():
    """pending 超 30 分钟未领取 → 过期标记 failed，不下发。"""
    cmd_id = server_executor.enqueue("list_dir", "F:/")
    old = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
    conn = connect()
    conn.execute("UPDATE executor_commands SET created_at=? WHERE id=?", (old, cmd_id))
    conn.commit()
    conn.close()
    assert server_executor.get_pending() is None
    conn = connect()
    row = conn.execute(
        "SELECT status FROM executor_commands WHERE id=?", (cmd_id,)
    ).fetchone()
    conn.close()
    assert row["status"] == "failed"
