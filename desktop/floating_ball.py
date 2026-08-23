"""悬浮球：无边框透明圆形图标，可拖拽，点击展开聊天面板。"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

from chat_panel import ChatPanel


class FloatingBall(QWidget):
    SIZE = 56

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(self.SIZE, self.SIZE)
        self._drag_pos = None
        self.panel: ChatPanel | None = None

        # 默认位置：屏幕右下角
        from PySide6.QtWidgets import QApplication

        screen = self.screen() or QApplication.primaryScreen()
        geo = screen.availableGeometry()
        self.move(geo.right() - self.SIZE - 40, geo.bottom() - self.SIZE - 40)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(43, 92, 255, 220))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(2, 2, self.SIZE - 4, self.SIZE - 4)
        painter.setPen(QColor("white"))
        font = painter.font()
        font.setPixelSize(28)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignCenter, "🤖")

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            # 点击（无拖动）→ 展开聊天面板
            if self._drag_pos is not None and (event.globalPosition().toPoint() - self.frameGeometry().topLeft() - self._drag_pos).manhattanLength() < 5:
                self.toggle_panel()
            self._drag_pos = None

    def toggle_panel(self) -> None:
        if self.panel is None or not self.panel.isVisible():
            self.open_panel()
        else:
            self.panel.hide()

    def open_panel(self) -> None:
        if self.panel is None:
            self.panel = ChatPanel()
        self.panel.show()
        self.panel.raise_()
        # 面板出现在悬浮球旁边
        pos = self.frameGeometry().topLeft()
        self.panel.move(pos.x() - self.panel.width() + self.SIZE, pos.y() - self.panel.height() + self.SIZE)
