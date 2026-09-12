#!/usr/bin/env python3
"""群消息收集量报表：用于评估落库增长速度与是否需要保留期清理。

只输出统计与体积，不打印任何消息原文或完整群号/QQ 号，可安全贴进对话或文档。

用法：
    python3 scripts/group_data_report.py                 # 默认读生产库
    python3 scripts/group_data_report.py --db 路径        # 指定库
    python3 scripts/group_data_report.py --hours 48       # 统计近 48 小时
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DB = "/opt/personal-ai-assistant/server/data/assistant.db"


def _mask(value: str) -> str:
    """群号/QQ 号只留前 3 位，避免报表泄露完整标识。"""
    text = str(value or "")
    return (text[:3] + "***") if len(text) > 3 else text


def _human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--hours", type=int, default=48)
    args = parser.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"数据库不存在: {db}")
        return 1

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).isoformat()

        total = conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
        group_total = conn.execute(
            "SELECT COUNT(*) c FROM memories WHERE group_id != ''"
        ).fetchone()["c"]
        recent = conn.execute(
            "SELECT COUNT(*) c FROM memories WHERE group_id != '' AND ts >= ?", (cutoff,)
        ).fetchone()["c"]

        print(f"报表时间: {datetime.now().strftime('%F %T')}")
        print(f"数据库体积: {_human(db.stat().st_size)}")
        print(f"记忆总数 {total} 条（群 {group_total} / 私聊 {total - group_total}）")
        print(f"近 {args.hours} 小时新增群消息: {recent} 条")
        if recent:
            per_day = recent / max(args.hours, 1) * 24
            print(f"  折算日增: 约 {per_day:.0f} 条/天；月增: 约 {per_day * 30:.0f} 条/月")

        print("\n分群统计:")
        rows = conn.execute(
            """SELECT group_id, COUNT(*) c, MIN(ts) first_ts, MAX(ts) last_ts
               FROM memories WHERE group_id != '' GROUP BY group_id ORDER BY c DESC"""
        ).fetchall()
        if not rows:
            print("  （暂无群消息）")
        for r in rows:
            print(
                f"  群{_mask(r['group_id'])}: {r['c']} 条"
                f"（{r['first_ts'][:16]} ~ {r['last_ts'][:16]}）"
            )

        print("\n画像积累:")
        prof = conn.execute(
            "SELECT user_id, COUNT(*) c FROM profile GROUP BY user_id ORDER BY c DESC"
        ).fetchall()
        for r in prof:
            uid = r["user_id"] if r["user_id"] == "owner" else _mask(r["user_id"])
            print(f"  {uid}: {r['c']} 个维度")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
