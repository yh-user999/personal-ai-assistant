"""Windows 行为采集器 · 守护进程入口

三通道采集 → 线程安全队列 → 批量推送服务器（断网落盘重试）。
用法: python main.py
"""
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 仓库根：common 共享包

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# 确保日志目录存在（FileHandler 需要）
(Path(__file__).resolve().parent / "logs").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        # 文件日志：开机自启（pythonw 无控制台）时仍可排查
        logging.FileHandler(
            Path(__file__).resolve().parent / "logs" / "collector.log",
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("collector")

from config import settings  # noqa: E402
from window_monitor import WindowMonitor  # noqa: E402
from browser_history import BrowserHistoryCollector  # noqa: E402
from git_scanner import GitScanner  # noqa: E402
from pusher import EventPusher  # noqa: E402
from executor import Executor  # noqa: E402


async def main() -> None:
    # 缓存目录统一走 settings.cache_path（绝对路径，以 collector/ 为基准）。
    # 此前 pusher 用默认 "./cache"、两个游标各自写 "./cache/xxx.json"，
    # 都按进程 CWD 解析——开机自启时 CWD 是 System32，队列与游标写去别处。
    cache = settings.cache_path
    cache.mkdir(parents=True, exist_ok=True)
    pusher = EventPusher(
        settings.server_url,
        token=settings.api_token,
        privacy_filter=settings.privacy_filter,
        cache_dir=str(cache),
    )

    collectors = []
    if settings.collect_window:
        collectors.append(WindowMonitor(pusher, interval=settings.window_interval))
        pusher.register_channel("window", settings.window_interval)
    if settings.collect_browser:
        collectors.append(
            BrowserHistoryCollector(
                pusher,
                interval=settings.browser_interval,
                cursor_file=str(cache / "browser_cursor.json"),
            )
        )
        pusher.register_channel("browser", settings.browser_interval)
    if settings.collect_git:
        collectors.append(
            GitScanner(
                pusher,
                repos=settings.git_repos,
                interval=settings.git_interval,
                cursor_file=str(cache / "git_cursor.json"),
            )
        )
        pusher.register_channel("git", settings.git_interval)

    if not collectors:
        logger.warning("没有启用的采集通道，检查 .env 配置")
        return

    logger.info("采集器启动：%d 个通道 → %s", len(collectors), settings.server_url)
    executor = Executor(settings.server_url, token=settings.api_token)
    tasks = [
        asyncio.create_task(pusher.run()),
        asyncio.create_task(pusher.heartbeat()),
        # 执行器（第 11 课）：轮询服务器指令 → 执行"打开/列目录/读文件"
        asyncio.create_task(executor.run()),
        *[asyncio.create_task(c.run()) for c in collectors],
    ]
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        pass
    finally:
        for c in collectors:
            c.stop()
        await executor.aclose()
        await pusher.stop()


if __name__ == "__main__":
    asyncio.run(main())
