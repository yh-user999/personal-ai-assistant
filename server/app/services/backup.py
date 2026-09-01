"""每日自动备份：SQLite 热备份 + 完整性校验 + 压缩 + 日备/周备滚动。

资源设计（个人服务器友好）：
- 03:00 执行（避开周报 21:00 / 整合 4h 周期的高峰）
- 用 sqlite3.Connection.backup() 热备份——WAL 模式下直接复制文件会拷出
  不一致数据，backup API 保证一致性且增量页复制
- 备份后立即校验：PRAGMA integrity_check + 关键表可查。
  "从不验证的备份" = 不知道能不能恢复的备份，这是备份最常见的失效方式。
- gzip 压缩后保留：向量数据占了库的绝大部分，压缩比可观
- 滚动策略：近 3 份日备 + 4 份周备（周一那份留作周备）
  旧实现"保留 7 份日备"实测已占 461MB（注释还写着"约 60MB"），
  纯日备在库变大后既占空间又没有跨周回溯能力。
- 备份前检查磁盘剩余 <1GB 则跳过并告警
"""
import gzip
import logging
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import settings

logger = logging.getLogger("assistant.backup")

TZ = ZoneInfo("Asia/Shanghai")
KEEP_DAILY = 3            # 保留最近 3 份日备
KEEP_WEEKLY = 4           # 保留最近 4 份周备（每周一那份晋升为周备）
MIN_FREE_BYTES = 1 << 30  # 磁盘剩余低于 1GB 跳过备份
# 校验时必须能查通的表（缺任何一张说明备份不可用）
VERIFY_TABLES = ("memories", "facts", "knowledge_chunks")


def backup_dir() -> Path:
    return settings.db_file.parent / "backups"


def _verify(path: Path) -> tuple[bool, str]:
    """校验备份文件可打开、结构完整、关键表可查。

    只做 integrity_check 不够：它查的是页级结构，表被截断/schema 缺失
    也可能"通过"。这里额外对关键表跑一次 COUNT。
    """
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        return False, f"无法打开：{e}"
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            return False, f"integrity_check 未通过：{row[0] if row else '无返回'}"
        counts = []
        for t in VERIFY_TABLES:
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            counts.append(f"{t}={n}")
        return True, "、".join(counts)
    except sqlite3.Error as e:
        return False, f"关键表查询失败：{e}"
    finally:
        conn.close()


def _compress(src: Path, dest: Path) -> None:
    """gzip 压缩（流式，不把整库读进内存）。"""
    with open(src, "rb") as fin, gzip.open(dest, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, length=1 << 20)


def _prune(dest_dir: Path) -> int:
    """滚动清理：保留最近 KEEP_DAILY 份日备 + KEEP_WEEKLY 份周备。

    周备 = 文件名带 -weekly 后缀（备份日为周一时打上）。两类各自独立计数，
    这样"连续三天"与"跨四周"两种回溯需求都能同时满足。
    """
    all_gz = sorted(dest_dir.glob("assistant-*.db.gz"))
    weekly = [f for f in all_gz if f.name.endswith("-weekly.db.gz")]
    daily = [f for f in all_gz if not f.name.endswith("-weekly.db.gz")]

    removed = 0
    for group, keep in ((weekly, KEEP_WEEKLY), (daily, KEEP_DAILY)):
        for old in group[:-keep]:
            old.unlink(missing_ok=True)
            removed += 1
    # 清掉旧版未压缩备份（升级前遗留的 .db，不会再更新，白占几百 MB）
    for legacy in dest_dir.glob("assistant-*.db"):
        legacy.unlink(missing_ok=True)
        removed += 1
    return removed


def run_daily_backup() -> dict:
    """执行一次备份（热备 → 校验 → 压缩 → 滚动清理）。返回统计信息。"""
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
    now = datetime.now(TZ)
    stamp = now.strftime("%Y%m%d")
    # 周一的备份晋升为周备（保留更久，提供跨周回溯）
    suffix = "-weekly" if now.weekday() == 0 else ""
    final = dest_dir / f"assistant-{stamp}{suffix}.db.gz"

    # 热备份到临时文件：校验通过才压缩落位，避免"半个损坏备份覆盖好备份"
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(dest_dir), suffix=".tmp")
    tmp = Path(tmp_name)
    os.close(tmp_fd)  # sqlite3 自己打开该路径，这里只需要一个独占的文件名
    try:
        src_conn = sqlite3.connect(str(src))
        try:
            dest_conn = sqlite3.connect(str(tmp))
            try:
                src_conn.backup(dest_conn)
            finally:
                dest_conn.close()
        finally:
            src_conn.close()

        ok, detail = _verify(tmp)
        if not ok:
            logger.error("备份校验失败，已丢弃本次备份：%s", detail)
            return {"skipped": True, "reason": f"校验失败：{detail}"}

        raw_mb = tmp.stat().st_size / (1 << 20)
        _compress(tmp, final)
    finally:
        # 连 -wal / -shm 一起清：sqlite 打开临时库时会建这两个旁挂文件，
        # 只删主文件会在备份目录里越积越多。
        for leftover in (tmp, Path(f"{tmp}-wal"), Path(f"{tmp}-shm")):
            leftover.unlink(missing_ok=True)

    removed = _prune(dest_dir)
    size_mb = final.stat().st_size / (1 << 20)
    logger.info(
        "备份完成: %s（%.1fMB，原始 %.1fMB，压缩率 %.0f%%），校验 %s，清理 %d 份",
        final.name, size_mb, raw_mb, (1 - size_mb / raw_mb) * 100 if raw_mb else 0,
        detail, removed,
    )
    return {
        "backup": final.name,
        "size_mb": round(size_mb, 2),
        "raw_mb": round(raw_mb, 2),
        "verified": detail,
        "removed": removed,
    }
