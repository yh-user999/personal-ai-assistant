"""悬浮机器人：无边框透明窗口，可拖拽，点击展开聊天面板。

形象：QPainter 自绘机器人（天线 + 圆角头 + LED 眼睛 + 状态指示灯）。
动画：呼吸浮动（整体缓慢缩放）+ 随机眨眼。
换肤：把 SVG 放到 desktop/assets/robot.svg 会自动替换为素材渲染（v2 扩展位）。
"""
import math
import random
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

from chat_panel import ChatPanel

# 状态 → 指示灯颜色（可扩展：thinking 时联动聊天）
STATE_COLORS = {
    "idle": "#60a5fa",      # 蓝
    "online": "#34d399",    # 绿
    "thinking": "#fbbf24",  # 琥珀
    "error": "#f87171",     # 红
}

ASSET_SVG = Path(__file__).resolve().parent / "assets" / "robot.svg"


class FloatingBall(QWidget):
    SIZE = 72

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(self.SIZE, self.SIZE)
        self._drag_pos = None
        self.panel: ChatPanel | None = None
        self.state = "idle"

        # 动画状态
        self._phase = 0.0            # 呼吸相位
        self._blink = 0.0            # 眨眼进度 0..1
        self._blink_cd = random.uniform(3.0, 5.5)  # 距下次眨眼秒数

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)        # 20fps 足够

        # 默认位置：屏幕右下角
        screen = self.screen() or QApplication.primaryScreen()
        geo = screen.availableGeometry()
        self.move(geo.right() - self.SIZE - 40, geo.bottom() - self.SIZE - 40)

    # ── 对外接口 ───────────────────────────────────────────

    def set_state(self, state: str) -> None:
        """切换状态指示灯颜色：idle/online/thinking/error。"""
        if state in STATE_COLORS:
            self.state = state
            self.update()

    # ── 动画 ───────────────────────────────────────────────

    def _tick(self) -> None:
        self._phase += 0.06
        # 眨眼倒计时
        self._blink_cd -= 0.05
        if self._blink_cd <= 0 and self._blink == 0:
            self._blink = 0.01
        if self._blink > 0:
            self._blink += 0.09
            if self._blink >= 1:
                self._blink = 0
                self._blink_cd = random.uniform(3.0, 5.5)
        self.update()

    # ── 绘制 ───────────────────────────────────────────────

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # 呼吸浮动：围绕中心缩放
        scale = 1 + 0.03 * math.sin(self._phase)
        cx = self.SIZE / 2
        painter.translate(cx, cx)
        painter.scale(scale, scale)
        painter.translate(-cx, -cx)

        # 光晕底（若隐若现，随呼吸）
        halo = QColor("#2b5cff")
        halo.setAlpha(int(28 + 18 * math.sin(self._phase)))
        painter.setBrush(halo)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(10, 10, self.SIZE - 20, self.SIZE - 20)

        # ── 身体（先画，被头压住上半）──
        body = QColor("#23262f")
        body.setAlpha(235)
        painter.setBrush(body)
        painter.setPen(QPen(QColor("#3a3f4b"), 1))
        painter.drawRoundedRect(23, 50, 26, 16, 8, 8)

        # 指示灯（状态色）
        painter.setBrush(QColor(STATE_COLORS.get(self.state, STATE_COLORS["idle"])))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(33, 55, 6, 6)

        # ── 天线 ──
        painter.setPen(QPen(QColor("#4b5563"), 2))
        painter.drawLine(36, 12, 36, 6)
        antenna = QColor("#4d7cff")
        if self.state == "thinking":
            antenna = QColor("#fbbf24")
        painter.setBrush(antenna)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(33, 3, 6, 6)

        # ── 头部 ──
        head = QColor("#23262f")
        head.setAlpha(240)
        painter.setBrush(head)
        painter.setPen(QPen(QColor("#3a3f4b"), 1))
        painter.drawRoundedRect(14, 12, 44, 38, 12, 12)

        # ── 眼睛（LED 大眼 + 高光；眨眼 = 高度压扁）──
        eye_h = 11.0 * (1.0 - 0.85 * abs(math.sin(self._blink * math.pi)))
        eye_w = 11.0
        eye_color = QColor("#4d7cff") if self.state != "thinking" else QColor("#fbbf24")
        painter.setBrush(eye_color)
        painter.setPen(Qt.NoPen)
        for ex in (22, 39):
            painter.drawEllipse(int(ex), int(24 + (11 - eye_h) / 2), int(eye_w), max(2, int(eye_h)))
        # 高光
        if self._blink == 0:
            painter.setBrush(QColor(255, 255, 255, 160))
            for ex in (25, 42):
                painter.drawEllipse(ex, 26, 3, 3)

        # ── 嘴巴（随状态：微笑 / 思考圆 / 平线）──
        painter.setPen(QPen(QColor("#8b93a3"), 2))
        if self.state == "thinking":
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(33, 38, 6, 6)
        else:
            painter.drawArc(30, 36, 12, 9, 200 * 16, 140 * 16)

    # ── 交互 ───────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
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
            self.panel = ChatPanel(ball=self)
        self.panel.show()
        self.panel.raise_()
        pos = self.frameGeometry().topLeft()
        self.panel.move(pos.x() - self.panel.width() + self.SIZE, pos.y() - self.panel.height() + self.SIZE)
