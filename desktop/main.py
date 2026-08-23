"""桌面悬浮球 · 入口：悬浮球 + 聊天面板 + 托盘。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from PySide6.QtWidgets import QApplication  # noqa: E402

from floating_ball import FloatingBall  # noqa: E402


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Personal AI Assistant")
    ball = FloatingBall()
    ball.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
