"""最近消息接口测试。"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.models.database import init_db, connect, reset_connections  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """独立临时库。原实现删的是 /tmp 里那个从未被真正使用的文件——
    DB_PATH 环境变量隔离无效，init_db 实际落在生产库上。"""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    reset_connections()  # 长驻连接缓存握着旧库句柄，切库后必须丢弃
    init_db()            # tmp_path 每个用例都是新目录，无需再手工删文件
    yield
    reset_connections()


def _seed():
    conn = connect()
    conn.execute(
        "INSERT INTO memories (sender, content, ts) VALUES ('user','第一条','2026-08-25T10:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO memories (sender, content, ts) VALUES ('assistant','第二条','2026-08-25T10:01:00+00:00')"
    )
    conn.commit()
    conn.close()


def test_recent_messages_ordered_asc():
    """验证正序返回 + 我们插入的两条都在且顺序正确。
    注意：settings 单例导致测试共享一个库，不断言总条数。"""
    _seed()
    with TestClient(app) as client:
        r = client.get("/api/messages?limit=200")
        assert r.status_code == 200
        contents = [m["content"] for m in r.json()["messages"]]
        assert "第一条" in contents and "第二条" in contents
        assert contents.index("第一条") < contents.index("第二条")  # 正序：早的在前


def test_recent_messages_limit():
    _seed()
    with TestClient(app) as client:
        r = client.get("/api/messages?limit=1")
        assert len(r.json()["messages"]) == 1
