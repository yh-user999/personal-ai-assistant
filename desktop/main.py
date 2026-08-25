"""桌面悬浮机器人 · 入口：悬浮球 + 聊天面板。

退出方式：右键机器人 → 退出；或 Ctrl+C（干净退出）。
自启排查：所有异常写入 logs/desktop.log（pythonw 无窗口时也能查）。
"""
import logging
import signal
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# 黑匣子日志：开机自启（pythonw 无控制台）时排查唯一依据
_LOG_DIR = Path(__file__).resolve().parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_LOG_DIR / "desktop.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("desktop")


def _excepthook(exc_type, exc_value, exc_tb):
    """未捕获异常写入黑匣子（pythonw 下不打印，只能靠文件）。"""
    logger.critical(
        "未捕获异常:\n%s",
        "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
    )


sys.excepthook = _excepthook

from PySide6.QtWidgets import QApplication  # noqa: E402

from floating_ball import FloatingBall  # noqa: E402
from tray import TrayIcon  # noqa: E402


def main() -> None:
    logger.info("机器人启动中…")
    app = QApplication(sys.argv)
    app.setApplicationName("Personal AI Assistant")
    app.setQuitOnLastWindowClosed(False)  # 面板关闭不退出，托盘常驻
    # Ctrl+C 干净退出（Windows 控制台场景）：把 SIGINT 转成 Qt 退出
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    ball = FloatingBall()
    ball.show()
    tray = TrayIcon(ball)
    tray.show()
    ball._tray = tray  # 保持引用防止被回收
    logger.info("机器人已就绪")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
