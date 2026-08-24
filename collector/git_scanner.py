"""git 提交扫描：定时对配置的项目目录跑 `git log --since=<游标>` 增量采集。

游标持久化在 cache/git_cursor.json，按仓库记录上次扫描时间。
"""
import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from pusher import EventPusher


class GitScanner:
    def __init__(self, pusher: EventPusher, repos: str = "", interval: float = 900.0,
                 cursor_file: str = "./cache/git_cursor.json"):
        self.pusher = pusher
        self.repos = [r.strip() for r in repos.split(",") if r.strip()]
        self.interval = interval
        self.cursor_file = Path(cursor_file)
        self.cursor_file.parent.mkdir(parents=True, exist_ok=True)
        self._cursors = self._load_cursors()
        self._running = True

    def _load_cursors(self) -> dict:
        try:
            return json.loads(self.cursor_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_cursors(self) -> None:
        self.cursor_file.write_text(json.dumps(self._cursors), encoding="utf-8")

    async def run(self) -> None:
        while self._running:
            try:
                await asyncio.to_thread(self._scan_all)
                self.pusher.report_health("git")
            except Exception as e:
                print(f"[git] 扫描失败: {e}")
            await asyncio.sleep(self.interval)

    def _scan_all(self) -> None:
        for repo in self.repos:
            repo_path = Path(repo)
            if not repo_path.exists():
                print(f"[git] 仓库不存在: {repo}")
                continue
            since = self._cursors.get(repo, "")
            try:
                cmd = ["git", "log", "--since=" + since, "--pretty=%H|%cI|%s", "--date=iso"]
                if not since:
                    # 首次：只取最近 7 天
                    cmd = ["git", "log", "--since=7 days ago", "--pretty=%H|%cI|%s", "--date=iso"]
                out = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=30)
                if out.returncode != 0:
                    continue
                commits = []
                latest = since
                for line in out.stdout.strip().splitlines():
                    if not line:
                        continue
                    parts = line.split("|", 2)
                    if len(parts) < 3:
                        continue
                    _hash, ts, subject = parts
                    commits.append({
                        "kind": "git_commit",
                        "name": repo_path.name,
                        "detail": subject[:100],
                        "start_ts": ts,
                    })
                    if ts > latest:
                        latest = ts
                for c in commits:
                    self.pusher.add_event(c)
                self._cursors[repo] = latest
            except Exception as e:
                print(f"[git] {repo} 扫描出错: {e}")
        self._save_cursors()

    def stop(self) -> None:
        self._running = False
        self.pusher.flush_to_disk()
