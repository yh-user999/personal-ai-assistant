"""聊天框动画头像：迷你版小月（从 chat_panel.py 拆出，跟随悬浮机器人换肤）。"""
import math
import random

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

import skins


class RobotAvatar(QWidget):
    """聊天框里的动画头像：迷你版小月（会眨眼/呼吸，思考时琥珀眼+环绕粒子）。"""

    SIZE = 36

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self._phase = 0.0
        self._blink = 0.0
        self._blink_cd = random.uniform(2.0, 4.0)
        self._thinking = False
        self.skin = skins.current_skin()  # bender / astro（跟随悬浮机器人换肤）
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(80)  # ~12fps：动画流畅且省电

    def set_thinking(self, on: bool) -> None:
        if self._thinking != on:
            self._thinking = on
            self.update()

    def _tick(self) -> None:
        self._phase += 0.06
        self._blink_cd -= 0.08
        if self._blink_cd <= 0 and self._blink == 0:
            self._blink = 0.01
        if self._blink > 0:
            self._blink += 0.09
            if self._blink >= 1:
                self._blink = 0
                self._blink_cd = random.uniform(2.0, 4.0)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # 呼吸微缩放
        scale = 1 + 0.02 * math.sin(self._phase)
        cx = self.SIZE / 2
        painter.translate(cx, cx)
        painter.scale(scale, scale)
        painter.translate(-cx, -cx)

        if self.skin == "astro":
            self._paint_astro(painter)
        elif self.skin == "classic":
            self._paint_classic(painter)
        else:
            self._paint_bender(painter)

    def _paint_classic(self, painter: QPainter) -> None:
        """原版萌系迷你头像：暗色圆角头 + 双 LED 眼 + 微笑。"""
        accent = QColor("#fbbf24") if self._thinking else QColor("#4d7cff")

        g = QLinearGradient(3, 3, 33, 33)
        g.setColorAt(0.0, QColor("#4a5266"))
        g.setColorAt(1.0, QColor("#1a1d24"))
        painter.setBrush(g)
        painter.setPen(QPen(QColor("#4a5264"), 1))
        painter.drawRoundedRect(4, 4, 28, 26, 9, 9)

        painter.setPen(QPen(QColor("#4b5563"), 2))
        painter.drawLine(18, 4, 18, 1)
        painter.setPen(Qt.NoPen)
        painter.setBrush(accent)
        painter.drawEllipse(16, 0, 4, 4)

        eye_h = 6.0 * (1.0 - 0.85 * abs(math.sin(self._blink * math.pi)))
        painter.setBrush(accent)
        for ex in (10, 21):
            painter.drawEllipse(ex, int(12 + (6 - eye_h) / 2), 5, max(1, int(eye_h)))

        painter.setPen(QPen(QColor("#9aa3b5"), 1.5, Qt.SolidLine, Qt.RoundCap))
        painter.setBrush(Qt.NoBrush)
        if self._thinking:
            painter.drawEllipse(16, 24, 4, 4)
        else:
            painter.drawArc(14, 22, 8, 6, 200 * 16, 140 * 16)

        if self._thinking:
            painter.setPen(Qt.NoPen)
            for i in range(3):
                ang = self._phase * 1.8 + i * 2.094
                px = 18 + 9 * math.cos(ang)
                py = 15 + 9 * math.sin(ang)
                dot = QColor(accent)
                dot.setAlpha(150)
                painter.setBrush(dot)
                painter.drawEllipse(int(px), int(py), 2, 2)

    def _paint_bender(self, painter: QPainter) -> None:
        """班德风迷你头像：金属灰桶形头 + 视窗眼 + 格栅嘴。"""
        accent = QColor("#fbbf24") if self._thinking else QColor("#4d7cff")

        g = QLinearGradient(3, 3, 33, 33)
        g.setColorAt(0.0, QColor("#a9b2c4"))
        g.setColorAt(0.5, QColor("#6b7488"))
        g.setColorAt(1.0, QColor("#3a414e"))

        head = QPainterPath()
        head.moveTo(8, 15)
        head.quadTo(8, 6, 18, 6)
        head.quadTo(28, 6, 28, 15)
        head.lineTo(31, 25)
        head.quadTo(31, 28, 27, 28)
        head.lineTo(9, 28)
        head.quadTo(5, 28, 5, 25)
        head.lineTo(8, 15)
        head.closeSubpath()
        painter.setBrush(g)
        painter.setPen(QPen(QColor("#565e70"), 1))
        painter.drawPath(head)

        painter.setPen(QPen(QColor("#565e70"), 2))
        painter.drawLine(18, 6, 18, 2)
        painter.setPen(Qt.NoPen)
        painter.setBrush(accent)
        painter.drawEllipse(16, 0, 4, 4)

        visor_h = 5.0 * (1.0 - 0.85 * abs(math.sin(self._blink * math.pi)))
        glow = QColor(accent)
        glow.setAlpha(55)
        painter.setBrush(glow)
        painter.drawRoundedRect(7, 12, 22, 10, 5, 5)
        painter.setBrush(QColor("#20242c"))
        painter.drawRoundedRect(9, int(16 - visor_h / 2) + 1, 18, int(visor_h) - 2, 3, 3)
        painter.setBrush(QColor(accent))
        painter.drawRoundedRect(10, int(16 - visor_h / 2) + 2, 16, max(1, int(visor_h)) - 4, 2, 2)

        painter.setPen(QPen(QColor("#565e70"), 1.2, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(12, 23, 24, 23)
        painter.drawLine(12, 25, 24, 25)

        if self._thinking:
            painter.setPen(Qt.NoPen)
            for i in range(3):
                ang = self._phase * 1.8 + i * 2.094
                px = 18 + 9 * math.cos(ang)
                py = 15 + 9 * math.sin(ang)
                dot = QColor(accent)
                dot.setAlpha(150)
                painter.setBrush(dot)
                painter.drawEllipse(int(px), int(py), 2, 2)

    def _paint_astro(self, painter: QPainter) -> None:
        """白色宇航员风迷你头像：白盔 + 琥珀面罩 + 黑圆眼（眨眼=闭眼线）。"""
        g = QLinearGradient(4, 3, 32, 29)
        g.setColorAt(0.0, QColor("#ffffff"))
        g.setColorAt(0.55, QColor("#dfe4ec"))
        g.setColorAt(1.0, QColor("#b9c1ce"))

        # 白盔 + 小圆耳
        painter.setBrush(g)
        painter.setPen(QPen(QColor("#9aa3b5"), 1))
        painter.drawRoundedRect(4, 3, 28, 26, 13, 13)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#dfe4ec"))
        painter.drawEllipse(0, 14, 7, 7)
        painter.drawEllipse(29, 14, 7, 7)

        # 天线
        painter.setPen(QPen(QColor("#9aa3b5"), 2))
        painter.drawLine(18, 3, 18, 0)
        accent = QColor("#fbbf24") if self._thinking else QColor("#4d7cff")
        painter.setPen(Qt.NoPen)
        painter.setBrush(accent)
        painter.drawEllipse(16, -1, 4, 4)

        # 面罩（thinking 发光）
        if self._thinking:
            glow = QColor(accent)
            glow.setAlpha(70)
            painter.setBrush(glow)
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(8, 8, 20, 18, 6, 6)
        painter.setBrush(QColor("#f2b33d"))
        painter.setPen(QPen(QColor("#b8822a"), 1))
        painter.drawRoundedRect(10, 10, 16, 14, 5, 5)

        # 眼睛：黑圆眼 + 高光（眨眼 = 闭眼横线；思考 = 上翻）
        eye_y = 15 if not self._thinking else 13
        painter.setBrush(QColor("#1a1d24"))
        painter.setPen(Qt.NoPen)
        if self._blink > 0:
            painter.setPen(QPen(QColor("#1a1d24"), 1.6, Qt.SolidLine, Qt.RoundCap))
            painter.setBrush(Qt.NoBrush)
            painter.drawLine(12, eye_y, 16, eye_y)
            painter.drawLine(20, eye_y, 24, eye_y)
        else:
            painter.drawEllipse(12, eye_y, 4, 4)
            painter.drawEllipse(20, eye_y, 4, 4)
            painter.setBrush(QColor(255, 255, 255, 210))
            painter.drawEllipse(13, eye_y + 1, 1.5, 1.5)
            painter.drawEllipse(21, eye_y + 1, 1.5, 1.5)

        # 嘴巴：短横线 / 思考 O
        painter.setPen(QPen(QColor("#8a5a14"), 1.2, Qt.SolidLine, Qt.RoundCap))
        if self._thinking:
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(16.5, 20, 3, 3)
        else:
            painter.drawLine(15, 21, 21, 21)

        # 思考环绕粒子
        if self._thinking:
            painter.setPen(Qt.NoPen)
            for i in range(3):
                ang = self._phase * 1.8 + i * 2.094
                px = 18 + 9 * math.cos(ang)
                py = 15 + 9 * math.sin(ang)
                dot = QColor(accent)
                dot.setAlpha(150)
                painter.setBrush(dot)
                painter.drawEllipse(int(px), int(py), 2, 2)
