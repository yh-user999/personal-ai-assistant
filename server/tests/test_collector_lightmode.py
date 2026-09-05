"""采集器轻量模式回归：行为通道默认关（心跳+执行器保留）、聊天行为注入默认关。"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))  # collector 与 common 共享包

from app.config import settings
from app.models.database import connect, init_db, reset_connections


def test_collector_channel_defaults_off():
    """行为采集三通道默认关闭——轻量模式（心跳+执行器仍在 main 里无条件启动）。"""
    from collector.config import CollectorSettings

    s = CollectorSettings(_env_file=None)
    assert s.collect_window is False
    assert s.collect_browser is False
    assert s.collect_git is False


def test_server_behavior_inject_default_off():
    assert settings.behavior_inject_enabled is False


def test_heartbeat_and_executor_unconditional():
    """main.py 三通道全关时不再提前 return——心跳与执行器必须无条件启动。

    通过源码断言（防未来有人把轻量模式的分支改回提前 return）：
    main() 中 pusher.heartbeat() 与 executor.run() 的 task 创建必须位于
    "没有启用通道"的提前返回之后、无条件执行。
    """
    src = (REPO_ROOT / "collector" / "main.py").read_text(encoding="utf-8")
    # 不允许存在"没有启用通道 → return"的旧守卫
    assert "没有启用的采集通道，检查 .env 配置" not in src
    assert 'logger.info("行为采集通道已全部关闭——仅心跳+执行器模式")' in src
    # 心跳与执行器在分支之外无条件创建
    assert "asyncio.create_task(pusher.heartbeat())" in src
    assert "asyncio.create_task(executor.run())" in src


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    reset_connections()
    init_db()
    yield
    reset_connections()


def test_chat_no_behavior_injection_by_default(db_env, monkeypatch):
    """默认关闭：即使有行为事件，聊天 prompt 也不注入"当前状态"。"""
    import app.api.chat as _chat_api
    import app.core.embedding as _embedding

    conn = connect()
    conn.execute(
        "INSERT INTO behavior_events (kind, name, detail, start_ts) "
        "VALUES ('app_usage', 'Chrome', '', '2026-09-02T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    systems = []

    async def fake_chat(messages, **kwargs):
        systems.append(messages[0]["content"])
        return "好的。"

    monkeypatch.setattr(_chat_api.llm, "chat", fake_chat)

    async def fake_embed(texts):
        return [[0.0] * settings.embedding_dimension for _ in texts]

    monkeypatch.setattr(_embedding, "embed", fake_embed)

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/chat", json={"message": "在干嘛"})
        assert r.status_code == 200
    # 占位符被替换为"暂无行为数据"，且没有真实行为内容
    assert "（暂无行为数据）" in systems[0]
    assert "Chrome" not in systems[0]


def test_chat_behavior_injection_when_enabled(db_env, monkeypatch):
    """开关打开时恢复注入（保留重开能力）。"""
    import app.api.chat as _chat_api
    import app.core.embedding as _embedding

    monkeypatch.setattr(settings, "behavior_inject_enabled", True)

    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    conn = connect()
    conn.execute(
        "INSERT INTO behavior_events (kind, name, detail, start_ts) "
        "VALUES ('app_usage', 'Chrome', '', ?)",
        (recent,),
    )
    conn.commit()
    conn.close()

    systems = []

    async def fake_chat(messages, **kwargs):
        systems.append(messages[0]["content"])
        return "好的。"

    monkeypatch.setattr(_chat_api.llm, "chat", fake_chat)

    async def fake_embed(texts):
        return [[0.0] * settings.embedding_dimension for _ in texts]

    monkeypatch.setattr(_embedding, "embed", fake_embed)

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/chat", json={"message": "在干嘛"})
        assert r.status_code == 200
    assert "Chrome" in systems[0]
