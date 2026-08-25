"""最近消息接口测试。"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")
os.environ.setdefault("DB_PATH", "/tmp/test_messages.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.models.database import init_db, connect  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    db_file = Path("/tmp/test_messages.db")
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_file) + suffix).unlink(missing_ok=True)
    init_db()
    yield


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
