"""Windows 行为采集器 · 守护进程入口

采集三通道事件 → 批量推送服务器（断网本地缓存重试）。
用法: python main.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from config import settings  # noqa: E402
from window_monitor import WindowMonitor  # noqa: E402
from browser_history import BrowserHistoryCollector  # noqa: E402
from git_scanner import GitScanner  # noqa: E402
from pusher import EventPusher  # noqa: E402


async def main() -> None:
    pusher = EventPusher(settings.server_url, token=settings.collector_token)

    collectors = []
    if settings.collect_window:
        collectors.append(WindowMonitor(pusher, interval=settings.window_interval))
    if settings.collect_browser:
        collectors.append(BrowserHistoryCollector(pusher, interval=settings.browser_interval))
    if settings.collect_git:
        collectors.append(GitScanner(pusher, repos=settings.git_repos, interval=settings.git_interval))

    if not collectors:
        print("没有启用的采集通道，检查 .env 配置")
        return

    print(f"采集器启动：{len(collectors)} 个通道 → {settings.server_url}")
    tasks = [asyncio.create_task(c.run()) for c in collectors]
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        pass
    finally:
        for c in collectors:
            await c.stop()


if __name__ == "__main__":
    asyncio.run(main())
