"""桌面悬浮机器人 · 入口：悬浮球 + 聊天面板。

退出方式：右键机器人 → 退出；或 Ctrl+C（干净退出）。
"""
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from PySide6.QtWidgets import QApplication  # noqa: E402

from floating_ball import FloatingBall  # noqa: E402
from tray import TrayIcon  # noqa: E402


def main() -> None:
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
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
