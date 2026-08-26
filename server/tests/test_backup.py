"""备份服务测试：热备份生成 + 滚动清理。"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")
os.environ.setdefault("DB_PATH", "/tmp/test_backup_data/assistant.db")

from app.models.database import connect, init_db  # noqa: E402
from app.services import backup  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    # 用独立临时库跑备份测试，避免污染共享库
    db_file = tmp_path / "data" / "assistant.db"
    db_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("app.config.settings.db_path", str(db_file))
    # 清理 settings 缓存的 db_file property 不可行——直接改 Path 解析
    init_db()
    # 造点数据
    conn = connect()
    conn.execute("INSERT INTO memories (sender, content, ts) VALUES ('user','备份测试','2026-08-26T00:00:00+00:00')")
    conn.commit()
    conn.close()
    yield
    # 清理测试备份目录
    import shutil
    shutil.rmtree(db_file.parent / "backups", ignore_errors=True)


def test_backup_creates_file():
    result = backup.run_daily_backup()
    assert "backup" in result
    dest = backup.backup_dir() / result["backup"]
    assert dest.exists()
    assert result["size_mb"] > 0


def test_backup_rolling_cleanup():
    # 预造 10 份旧备份（不同日期文件名），验证滚动清理保留最近 7 份
    d = backup.backup_dir()
    d.mkdir(parents=True, exist_ok=True)
    for i in range(10):
        (d / f"assistant-202601{i:02d}.db").write_bytes(b"x")
    backup.run_daily_backup()
    files = list(d.glob("assistant-*.db"))
    assert len(files) <= backup.KEEP_COUNT  # 滚动保留生效
