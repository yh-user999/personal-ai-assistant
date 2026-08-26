"""每日自动备份：SQLite 热备份 + 滚动保留。

资源设计（个人服务器友好）：
- 03:00 执行（避开周报 21:00 / 整合 4h 周期的高峰）
- 用 sqlite3.Connection.backup() 热备份——WAL 模式下直接复制文件会拷出
  不一致数据，backup API 保证一致性且增量页复制（8MB 级秒完成）
- 保留最近 7 份（约 60MB），滚动删除
- 备份前检查磁盘剩余 <1GB 则跳过并告警
"""
import logging
import sqlite3
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import settings

logger = logging.getLogger("assistant.backup")

TZ = ZoneInfo("Asia/Shanghai")
KEEP_COUNT = 7           # 保留最近 7 份
MIN_FREE_BYTES = 1 << 30  # 磁盘剩余低于 1GB 跳过备份


def backup_dir() -> Path:
    return settings.db_file.parent / "backups"


def run_daily_backup() -> dict:
    """执行一次备份。返回统计信息。"""
    src = settings.db_file
    if not src.exists():
        return {"skipped": True, "reason": "数据库文件不存在"}

    # 磁盘空间检查
    free = shutil.disk_usage(src.parent).free
    if free < MIN_FREE_BYTES:
        logger.warning("磁盘剩余不足 1GB（%.1fGB），跳过备份", free / (1 << 30))
        return {"skipped": True, "reason": "磁盘空间不足"}

    dest_dir = backup_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(TZ).strftime("%Y%m%d")
    dest = dest_dir / f"assistant-{stamp}.db"

    # 热备份：源连接读、目标连接写，WAL 下也保证一致性
    src_conn = sqlite3.connect(str(src))
    try:
        dest_conn = sqlite3.connect(str(dest))
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()

    # 滚动清理：按文件名排序保留最近 KEEP_COUNT 份
    backups = sorted(dest_dir.glob("assistant-*.db"))
    removed = 0
    for old in backups[:-KEEP_COUNT]:
        old.unlink(missing_ok=True)
        removed += 1

    size_mb = dest.stat().st_size / (1 << 20)
    logger.info("备份完成: %s（%.1fMB），清理 %d 份旧备份", dest.name, size_mb, removed)
    return {"backup": dest.name, "size_mb": round(size_mb, 2), "removed": removed}
