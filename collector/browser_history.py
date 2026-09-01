"""浏览器历史采集：增量读取 Chrome/Edge 的 History SQLite。

做法：先把 History 文件复制到临时目录再读（避免文件锁），
按 last_visit_time 游标增量提取 URL/标题/时间。

隐私：只保留 域名 + 标题关键词（80 字符截断），不上传正文。
"""
import asyncio
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from pusher import EventPusher

logger = logging.getLogger("collector.browser")

def _local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "C:/Users/default/AppData/Local"))


def _roaming_appdata() -> Path:
    return Path(os.environ.get("APPDATA", "C:/Users/default/AppData/Roaming"))


# Chromium 系浏览器的 User Data 根目录（各 profile 是其下的子目录）。
# 此前只写死了 Chrome/Edge 的 Default profile——多 profile（工作/个人分离
# 是常见用法）与其他 Chromium 浏览器全部漏采。
_CHROMIUM_ROOTS = {
    "chrome": "Google/Chrome/User Data",
    "edge": "Microsoft/Edge/User Data",
    "brave": "BraveSoftware/Brave-Browser/User Data",
    "vivaldi": "Vivaldi/User Data",
    "chromium": "Chromium/User Data",
}

# Firefox 用完全不同的库（places.sqlite）与表结构（moz_places/moz_historyvisits），
# 时间戳也是 Unix 微秒而非 WebKit 纪元，故单独适配。
_FIREFOX_PROFILES_DIR = "Mozilla/Firefox/Profiles"

# WebKit 时间戳起点（1601-01-01）到 Unix 起点（1970-01-01）的微秒差
_WEBKIT_EPOCH = 11644473600000000

# 兼容旧引用（测试/脚本按名字导入）
CHROME_HISTORY = _local_appdata() / "Google/Chrome/User Data/Default/History"
EDGE_HISTORY = _local_appdata() / "Microsoft/Edge/User Data/Default/History"


def _webkit_to_iso(us: int) -> str:
    return datetime.fromtimestamp((us - _WEBKIT_EPOCH) / 1_000_000, timezone.utc).isoformat()


def _unix_us_to_iso(us: int) -> str:
    """Firefox 的 visit_date 是 Unix 微秒。"""
    return datetime.fromtimestamp(us / 1_000_000, timezone.utc).isoformat()


def discover_chromium_histories() -> list[tuple[str, Path]]:
    """枚举所有 Chromium 系浏览器的所有 profile 的 History 文件。

    返回 [(来源标签, History 路径)]，标签形如 'chrome/Default'、
    'edge/Profile 1'——事件里带上来源，便于区分工作/个人浏览。
    """
    out: list[tuple[str, Path]] = []
    base = _local_appdata()
    for name, rel in _CHROMIUM_ROOTS.items():
        root = base / rel
        if not root.is_dir():
            continue
        # profile 目录：Default、Profile 1、Profile 2…（其余目录没有 History）
        for profile_dir in sorted(root.iterdir()):
            if not profile_dir.is_dir():
                continue
            hist = profile_dir / "History"
            if hist.exists():
                out.append((f"{name}/{profile_dir.name}", hist))
    return out


def discover_firefox_histories() -> list[tuple[str, Path]]:
    """枚举 Firefox 各 profile 的 places.sqlite。"""
    out: list[tuple[str, Path]] = []
    root = _roaming_appdata() / _FIREFOX_PROFILES_DIR
    if not root.is_dir():
        return out
    for profile_dir in sorted(root.iterdir()):
        if not profile_dir.is_dir():
            continue
        places = profile_dir / "places.sqlite"
        if places.exists():
            out.append((f"firefox/{profile_dir.name}", places))
    return out


class BrowserHistoryCollector:
    def __init__(self, pusher: EventPusher, interval: float = 600.0, cursor_file: str = "./cache/browser_cursor.json"):
        self.pusher = pusher
        self.interval = interval
        self.cursor_file = Path(cursor_file)
        self.cursor_file.parent.mkdir(parents=True, exist_ok=True)
        # 每个来源（浏览器/profile）一个独立游标：共用单游标时，
        # 读完 chrome/Default 后游标已推到最新，其余 profile 的历史全被跳过。
        self._cursors: dict[str, int] = {}
        self._default_cursor = self._initial_cursor()
        self._load_cursor()
        self._running = True

    @staticmethod
    def _initial_cursor() -> int:
        """首次运行：只取最近 24h，避免历史全量灌入。"""
        return int((time.time() + _WEBKIT_EPOCH / 1_000_000) * 1_000_000) - 86400_000_000

    def _load_cursor(self) -> None:
        """读游标文件。兼容旧格式 {"cursor": N}（单游标）→ 作为各源初值。"""
        try:
            data = json.loads(self.cursor_file.read_text(encoding="utf-8"))
        except Exception:
            return
        if isinstance(data.get("sources"), dict):
            self._cursors = {k: int(v) for k, v in data["sources"].items()}
            self._default_cursor = int(data.get("cursor", self._default_cursor))
        elif "cursor" in data:
            # 旧版单游标：迁移为所有来源的共同起点，不重复采集历史
            self._default_cursor = int(data["cursor"])

    def _cursor_for(self, label: str) -> int:
        return self._cursors.get(label, self._default_cursor)

    def _save_cursor(self) -> None:
        self.cursor_file.write_text(
            json.dumps({"cursor": self._default_cursor, "sources": self._cursors}),
            encoding="utf-8",
        )

    async def run(self) -> None:
        while self._running:
            try:
                await asyncio.to_thread(self._collect_once)
                self.pusher.report_health("browser")
            except Exception as e:
                logger.warning("采集失败: %s", e)
            await asyncio.sleep(self.interval)

    def _collect_once(self) -> None:
        sources = discover_chromium_histories()
        firefox = discover_firefox_histories()
        if not sources and not firefox:
            logger.debug("未发现任何浏览器历史库")
        for label, history_path in sources:
            self._copy_and_read(label, history_path, "History", self._read_chromium)
        for label, places_path in firefox:
            self._copy_and_read(label, places_path, "places.sqlite", self._read_firefox)
        self._save_cursor()

    def _copy_and_read(self, label: str, src: Path, filename: str, reader) -> None:
        """复制到临时目录再读（原库被浏览器占用，直接读会锁失败）。"""
        tmp_dir = Path(tempfile.mkdtemp())
        tmp = tmp_dir / filename
        try:
            shutil.copy2(src, tmp)
            reader(tmp, label)
        except Exception as e:
            logger.warning("%s 读取失败: %s", label, e)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _read_chromium(self, db_path: Path, label: str) -> None:
        cursor = self._cursor_for(label)
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                """SELECT u.url, u.title, v.visit_time FROM urls u
                   JOIN visits v ON u.id = v.url
                   WHERE v.visit_time > ? ORDER BY v.visit_time LIMIT 500""",
                (cursor,),
            ).fetchall()
        finally:
            conn.close()
        for url, title, visit_time in rows:
            cursor = max(cursor, visit_time)
            self._emit(url, title, _webkit_to_iso(visit_time), label)
        self._cursors[label] = cursor

    def _read_firefox(self, db_path: Path, label: str) -> None:
        """Firefox：moz_places/moz_historyvisits，visit_date 是 Unix 微秒。

        游标统一按 WebKit 微秒存储（与 Chromium 同一套语义），
        故这里做双向换算，避免为 Firefox 单独维护一份游标文件。
        """
        cursor = self._cursor_for(label)
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                """SELECT p.url, p.title, v.visit_date FROM moz_places p
                   JOIN moz_historyvisits v ON p.id = v.place_id
                   WHERE v.visit_date > ? ORDER BY v.visit_date LIMIT 500""",
                (cursor - _WEBKIT_EPOCH,),
            ).fetchall()
        finally:
            conn.close()
        for url, title, visit_date in rows:
            cursor = max(cursor, visit_date + _WEBKIT_EPOCH)
            self._emit(url, title, _unix_us_to_iso(visit_date), label)
        self._cursors[label] = cursor

    def _emit(self, url: str, title: str, ts: str, label: str) -> None:
        domain = urlparse(url or "").netloc or "unknown"
        detail = (title or "")[:80]
        if not detail:
            return  # 无标题（如纯图片页）跳过
        self.pusher.add_event({
            "kind": "browser",
            "name": domain,
            "detail": detail,
            "start_ts": ts,
            # 来源 profile：区分工作/个人浏览器画像。
            # 必须是 dict——服务端 BehaviorEvent.meta 声明为 dict，
            # 传 JSON 字符串会让整批事件 422。
            "meta": {"source": label},
        })

    def stop(self) -> None:
        self._running = False
        self.pusher.flush_to_disk()
