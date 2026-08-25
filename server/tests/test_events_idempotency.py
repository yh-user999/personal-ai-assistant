"""事件接收幂等性测试：同一事件重复推送只入库一次。"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")
os.environ.setdefault("DB_PATH", "/tmp/test_events_dedup.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.models.database import init_db  # noqa: E402

BATCH = {
    "events": [
        {
            "kind": "browser",
            "name": "example.com",
            "detail": "某页面标题",
            "start_ts": "2026-08-25T10:00:00+00:00",
        },
        {
            "kind": "browser",
            "name": "example2.com",
            "detail": "另一页面",
            "start_ts": "2026-08-25T10:01:00+00:00",
        },
    ]
}


@pytest.fixture(autouse=True)
def fresh_db():
    db_file = Path("/tmp/test_events_dedup.db")
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_file) + suffix).unlink(missing_ok=True)
    init_db()
    yield


def test_duplicate_batch_skipped():
    with TestClient(app) as client:
        r1 = client.post("/api/events", json=BATCH)
        r2 = client.post("/api/events", json=BATCH)  # 完全重复的整批重推
        assert r1.json() == {"received": 2, "inserted": 2}
        assert r2.json() == {"received": 2, "inserted": 0}  # 幂等：第二次零入库


def test_partial_duplicate_only_inserts_new():
    with TestClient(app) as client:
        client.post("/api/events", json=BATCH)
        mixed = {
            "events": [
                BATCH["events"][0],  # 重复
                {"kind": "browser", "name": "new.com", "detail": "新页面",
                 "start_ts": "2026-08-25T10:02:00+00:00"},  # 新事件
            ]
        }
        r = client.post("/api/events", json=mixed)
        assert r.json() == {"received": 2, "inserted": 1}
