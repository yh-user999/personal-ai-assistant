"""浏览器历史采集：增量读取 Chrome/Edge 的 History SQLite。

做法：先把 History 文件复制到临时目录再读（避免文件锁），
按 last_visit_time 游标增量提取 URL/标题/时间。

隐私：只保留 域名 + 标题关键词（80 字符截断），不上传正文。
"""
import asyncio
import json
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from pusher import EventPusher

CHROME_HISTORY = Path(os.environ.get(
    "LOCALAPPDATA", "C:/Users/default/AppData/Local"
)) / "Google/Chrome/User Data/Default/History"

EDGE_HISTORY = Path(os.environ.get(
    "LOCALAPPDATA", "C:/Users/default/AppData/Local"
)) / "Microsoft/Edge/User Data/Default/History"

# WebKit 时间戳起点（1601-01-01）到 Unix 起点（1970-01-01）的微秒差
_WEBKIT_EPOCH = 11644473600000000


def _webkit_to_iso(us: int) -> str:
    return datetime.fromtimestamp((us - _WEBKIT_EPOCH) / 1_000_000, timezone.utc).isoformat()


class BrowserHistoryCollector:
    def __init__(self, pusher: EventPusher, interval: float = 600.0, cursor_file: str = "./cache/browser_cursor.json"):
        self.pusher = pusher
        self.interval = interval
        self.cursor_file = Path(cursor_file)
        self.cursor_file.parent.mkdir(parents=True, exist_ok=True)
        self._cursor = self._load_cursor()
        self._running = True

    def _load_cursor(self) -> int:
        try:
            return int(json.loads(self.cursor_file.read_text(encoding="utf-8")).get("cursor", 0))
        except Exception:
            # 首次运行：只取最近 24h，避免历史全量灌入
            return int((time.time() + _WEBKIT_EPOCH / 1_000_000) * 1_000_000) - 86400_000_000

    def _save_cursor(self, cursor: int) -> None:
        self.cursor_file.write_text(json.dumps({"cursor": cursor}), encoding="utf-8")

    async def run(self) -> None:
        while self._running:
            try:
                await asyncio.to_thread(self._collect_once)
            except Exception as e:
                print(f"[browser] 采集失败: {e}")
            await asyncio.sleep(self.interval)

    def _collect_once(self) -> None:
        for history_path in (CHROME_HISTORY, EDGE_HISTORY):
            if not history_path.exists():
                continue
            tmp = Path(tempfile.mkdtemp()) / "History"
            try:
                shutil.copy2(history_path, tmp)
                self._read(tmp)
            except Exception as e:
                print(f"[browser] {history_path.name} 读取失败: {e}")
            finally:
                shutil.rmtree(tmp.parent, ignore_errors=True)
        self._save_cursor(self._cursor)

    def _read(self, db_path: Path) -> None:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                """SELECT u.url, u.title, v.visit_time FROM urls u
                   JOIN visits v ON u.id = v.url
                   WHERE v.visit_time > ? ORDER BY v.visit_time LIMIT 500""",
                (self._cursor,),
            ).fetchall()
        finally:
            conn.close()
        for url, title, visit_time in rows:
            self._cursor = max(self._cursor, visit_time)
            domain = urlparse(url).netloc or "unknown"
            detail = (title or "")[:80]
            if not detail:
                continue  # 无标题（如纯图片页）跳过
            self.pusher.add_event({
                "kind": "browser",
                "name": domain,
                "detail": detail,
                "start_ts": _webkit_to_iso(visit_time),
            })

    async def stop(self) -> None:
        self._running = False
        await self.pusher.flush()
