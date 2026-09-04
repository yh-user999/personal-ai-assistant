"""备份服务测试：热备份 + 完整性校验 + 压缩 + 日备/周备滚动 + 真实可恢复性。

重点：备份最常见的失效方式是"从来没验证过能不能恢复"，
所以这里不只断言文件存在，而是真的解压 + 打开 + 查数据。
"""
import gzip
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")
os.environ.setdefault("DB_PATH", "/tmp/test_backup_data/assistant.db")

from app.models.database import connect, init_db, reset_connections  # noqa: E402
from app.services import backup  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    # 用独立临时库跑备份测试，避免污染共享库
    db_file = tmp_path / "data" / "assistant.db"
    db_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("app.config.settings.db_path", str(db_file))
    reset_connections()
    init_db()
    # 造点数据
    conn = connect()
    conn.execute(
        "INSERT INTO memories (sender, content, ts) "
        "VALUES ('user','备份测试','2026-08-26T00:00:00+00:00')"
    )
    conn.commit()
    yield
    reset_connections()
    shutil.rmtree(db_file.parent / "backups", ignore_errors=True)


def test_backup_creates_compressed_file():
    result = backup.run_daily_backup()
    assert "backup" in result, result
    dest = backup.backup_dir() / result["backup"]
    assert dest.exists()
    assert dest.name.endswith(".db.gz")  # 压缩后落位
    assert result["size_mb"] >= 0
    assert result["raw_mb"] > 0


def test_backup_is_verified_and_restorable(tmp_path):
    """核心断言：备份能解压、能打开、integrity_check 通过、数据在。"""
    result = backup.run_daily_backup()
    assert "memories=" in result["verified"]  # 校验步骤真的跑了

    gz = backup.backup_dir() / result["backup"]
    restored = tmp_path / "restored.db"
    with gzip.open(gz, "rb") as fin, open(restored, "wb") as fout:
        shutil.copyfileobj(fin, fout)

    conn = sqlite3.connect(str(restored))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        rows = conn.execute(
            "SELECT content FROM memories WHERE content='备份测试'"
        ).fetchall()
        assert len(rows) == 1  # 恢复出来的库里数据确实还在
    finally:
        conn.close()


def test_novel_project_backup_restores_end_to_end(tmp_path, monkeypatch):
    from app.novel.repository import SQLiteNovelRepository

    repo = SQLiteNovelRepository(owner_id="owner")
    repo.create_project("恢复小说", project_id="restore-project", slug="restore-book")
    repo.upsert_chapter("restore-project", "1", title="开端", content="小说正文", status="published")
    job = repo.create_job("restore-project", "1", "restore-job", "生成提示")
    repo.update_job(job.job_id, __import__("app.novel.domain", fromlist=["GenerationJobStatus"]).GenerationJobStatus.AWAITING_CONFIRMATION, draft_content="小说草稿")

    result = backup.run_daily_backup()
    restored = tmp_path / "restored-novel.db"
    restored_result = backup.restore_backup(backup.backup_dir() / result["backup"], restored)
    assert restored_result["restored"] == str(restored)

    conn = sqlite3.connect(str(restored))
    try:
        assert conn.execute("SELECT name FROM novel_projects WHERE project_id='restore-project'").fetchone()[0] == "恢复小说"
        assert conn.execute("SELECT content FROM novel_chapters WHERE project_id='restore-project'").fetchone()[0] == "小说正文"
        assert conn.execute("SELECT prompt FROM novel_generation_jobs WHERE job_id=?", (job.job_id,)).fetchone()[0] == "生成提示"
    finally:
        conn.close()


def test_restore_refuses_overwrite_without_flag(tmp_path):
    result = backup.run_daily_backup()
    restored = tmp_path / "existing.db"
    restored.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        backup.restore_backup(backup.backup_dir() / result["backup"], restored)


def test_backup_rejects_corrupt_gzip(monkeypatch):
    """压缩输出损坏时不得落位。"""
    monkeypatch.setattr(backup, "_verify_gzip", lambda p: (False, "模拟 gzip 损坏"))
    result = backup.run_daily_backup()
    assert result.get("skipped") is True
    assert "gzip 校验失败" in result["reason"]
    assert not list(backup.backup_dir().glob("*.db.gz"))


def test_backup_rejects_corrupt_snapshot(monkeypatch):
    """校验失败时必须丢弃备份，而不是让坏文件覆盖好备份。"""
    monkeypatch.setattr(backup, "_verify", lambda p: (False, "模拟损坏"))
    result = backup.run_daily_backup()
    assert result.get("skipped") is True
    assert "校验失败" in result["reason"]
    # 没有任何备份落位
    assert not list(backup.backup_dir().glob("*.db.gz"))


def test_backup_compression_failure_preserves_existing(monkeypatch):
    """压缩失败时不得覆盖同名的既有好备份。"""
    real_dt = backup.datetime
    class FakeDatetime(real_dt):
        @classmethod
        def now(cls, tz=None):
            return real_dt(2026, 1, 7, 3, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(backup, "datetime", FakeDatetime)
    d = backup.backup_dir()
    d.mkdir(parents=True, exist_ok=True)
    final = d / "assistant-20260107.db.gz"
    final.write_bytes(b"known-good")
    monkeypatch.setattr(backup, "_compress", lambda src, dest: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        backup.run_daily_backup()
    assert final.read_bytes() == b"known-good"


def test_backup_leaves_no_temp_files():
    """临时快照与其 -wal/-shm 旁挂文件都要清干净。"""
    backup.run_daily_backup()
    leftovers = [f.name for f in backup.backup_dir().iterdir() if ".tmp" in f.name]
    assert leftovers == []


def test_rolling_keeps_daily_and_weekly_separately():
    """日备与周备各自独立计数，跨周回溯能力不被日备挤掉。"""
    d = backup.backup_dir()
    d.mkdir(parents=True, exist_ok=True)
    for i in range(6):
        (d / f"assistant-2026010{i}.db.gz").write_bytes(b"x")
    for i in range(6):
        (d / f"assistant-2026020{i}-weekly.db.gz").write_bytes(b"x")

    backup.run_daily_backup()

    dailies = [f for f in d.glob("assistant-*.db.gz") if "-weekly" not in f.name]
    weeklies = list(d.glob("assistant-*-weekly.db.gz"))
    assert len(dailies) <= backup.KEEP_DAILY
    assert len(weeklies) <= backup.KEEP_WEEKLY
    assert weeklies, "周备不该被日备清理逻辑连带删除"


def test_rolling_removes_legacy_uncompressed():
    """升级前遗留的未压缩 .db 备份要清掉（线上实测占了 461MB）。"""
    d = backup.backup_dir()
    d.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (d / f"assistant-2026030{i}.db").write_bytes(b"legacy")
    backup.run_daily_backup()
    assert list(d.glob("assistant-*.db")) == []


def test_monday_backup_promoted_to_weekly(monkeypatch):
    """周一的备份带 -weekly 后缀（保留更久）。"""
    real_dt = backup.datetime

    class FakeDatetime(real_dt):
        @classmethod
        def now(cls, tz=None):
            # 2026-01-05 是周一
            return real_dt(2026, 1, 5, 3, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    monkeypatch.setattr(backup, "datetime", FakeDatetime)
    result = backup.run_daily_backup()
    assert result["backup"].endswith("-weekly.db.gz")


def test_non_monday_backup_is_daily(monkeypatch):
    real_dt = backup.datetime

    class FakeDatetime(real_dt):
        @classmethod
        def now(cls, tz=None):
            # 2026-01-07 是周三
            return real_dt(2026, 1, 7, 3, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    monkeypatch.setattr(backup, "datetime", FakeDatetime)
    result = backup.run_daily_backup()
    assert not result["backup"].endswith("-weekly.db.gz")
    assert result["backup"] == "assistant-20260107.db.gz"


def test_skips_when_disk_nearly_full(monkeypatch):
    fake = type("U", (), {"free": 100 * (1 << 20), "total": 0, "used": 0})()
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda p: fake)
    result = backup.run_daily_backup()
    assert result["skipped"] is True
    assert result["reason"] == "磁盘空间不足"


def test_verify_detects_missing_table(tmp_path):
    """_verify 不只看 integrity_check：关键表缺失也要判失败。"""
    empty = tmp_path / "empty.db"
    sqlite3.connect(str(empty)).close()  # 结构完整但没有业务表
    ok, detail = backup._verify(empty)
    assert ok is False
    assert "关键表" in detail
