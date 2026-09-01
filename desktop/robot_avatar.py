"""聊天框动画头像：迷你版小月（从 chat_panel.py 拆出，跟随悬浮机器人换肤）。"""
import math
import random

import robot_paint
import skins
from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget


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

    def _paint_particles(self, painter: QPainter, accent: QColor) -> None:
        """思考时环绕粒子（3 个小光点绕头转，与悬浮球同款）。"""
        if not self._thinking:
            return
        painter.setPen(Qt.NoPen)
        for i in range(3):
            ang = self._phase * 1.8 + i * 2.094
            px = 18 + 10 * math.cos(ang)
            py = 16 + 10 * math.sin(ang)
            dot = QColor(accent)
            dot.setAlpha(max(0, int(110 + 90 * math.sin(self._phase * 3 + i))))
            painter.setBrush(dot)
            painter.drawEllipse(QPointF(px, py), 1.6, 1.6)

    # 六角螺栓绘制已移到 robot_paint.draw_hex_bolt（与 floating_ball 共用）
    _draw_hex = staticmethod(robot_paint.draw_hex_bolt)

    def _paint_bender(self, painter: QPainter) -> None:
        """班德金属风 · 硬核机械版迷你头像：平顶切角壳 + 小内嵌屏 + 散热格栅。"""
        accent = QColor("#fbbf24") if self._thinking else QColor("#4d7cff")
        steel = QLinearGradient(5, 4, 31, 27)
        steel.setColorAt(0.0, QColor("#b7c0d4"))
        steel.setColorAt(0.5, QColor("#646e82"))
        steel.setColorAt(1.0, QColor("#2f3542"))
        stroke = QColor("#525a6b")
        dark = QColor("#12151c")
        recess = QColor("#232833")

        # 天线（方形光点）
        painter.setPen(QPen(stroke, 1.5))
        painter.drawLine(18, 4, 18, 1)
        glow = QColor(accent)
        glow.setAlpha(150 if self._thinking else 90)
        painter.setBrush(glow)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(14.5, -0.5, 7, 4.5)
        painter.setBrush(accent)
        painter.drawRect(16.5, 0.8, 3, 3)

        # 侧六角螺栓
        for bx in (3.2, 32.8):
            self._draw_hex(painter, bx, 15.5, 3.2, steel, stroke)

        # 头（平顶切角）
        head = QPainterPath()
        head.moveTo(8, 4)
        head.lineTo(28, 4)
        head.lineTo(31, 7)
        head.lineTo(31, 24)
        head.lineTo(28, 27)
        head.lineTo(8, 27)
        head.lineTo(5, 24)
        head.lineTo(5, 7)
        head.closeSubpath()
        painter.setBrush(steel)
        painter.setPen(QPen(stroke, 1))
        painter.drawPath(head)

        # 顶板拼缝 + 铆钉
        painter.setPen(QPen(QColor(0, 0, 0, 80), 1))
        painter.drawLine(7, 8, 29, 8)
        painter.setBrush(QColor("#2e3440"))
        painter.drawEllipse(QPointF(9, 6), 0.9, 0.9)
        painter.drawEllipse(QPointF(27, 6), 0.9, 0.9)

        # 内嵌显示屏
        painter.setBrush(recess)
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(10, 10.5, 16, 7.5, 1, 1)
        painter.setBrush(dark)
        painter.drawRoundedRect(10.8, 11.3, 14.4, 5.9, 1, 1)

        # LED 扫描眼
        eye_h = 2.6 * (1.0 - 0.85 * abs(math.sin(self._blink * math.pi)))
        eye_h = max(1.0, eye_h)
        if self._thinking:
            halo = QColor(accent)
            halo.setAlpha(60)
            painter.setBrush(halo)
            painter.drawRoundedRect(11.5, 11.8, 13, 4.8, 1, 1)
        painter.setBrush(QColor(accent))
        painter.drawRoundedRect(12, 14.3 - eye_h / 2, 12, eye_h, 0.8, 0.8)

        # 散热格栅
        painter.setBrush(recess)
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(12.5, 20, 11, 5, 1, 1)
        painter.setPen(QPen(QColor("#565e70"), 1))
        for gx in (14, 16.2, 18.4, 20.6, 22.8):
            painter.drawLine(gx, 21, gx, 24)

        # 颈 + 身体
        painter.setBrush(QColor("#2e3440"))
        painter.setPen(Qt.NoPen)
        painter.drawRect(15, 27, 6, 2.5)
        painter.setBrush(steel)
        painter.setPen(QPen(stroke, 1))
        painter.drawRoundedRect(12, 29, 12, 6, 1, 1)
        pulse = QColor(accent)
        pulse.setAlpha(140 + int(100 * math.sin(self._phase * 2)))
        painter.setBrush(pulse)
        painter.drawEllipse(QPointF(18, 32), 1.3, 1.3)

        self._paint_particles(painter, accent)

    def _paint_astro(self, painter: QPainter) -> None:
        """白色宇航员风 · 重制版迷你头像：圆球头盔 + 深色玻璃面罩 + 发光圆眼。"""
        accent = QColor("#fbbf24") if self._thinking else QColor("#4d7cff")
        stroke = QColor("#9aa5b8")
        yellow = QColor("#f5c518")

        # 天线（小月签名）
        painter.setPen(QPen(stroke, 1.5))
        painter.drawLine(18, 5.5, 18, 2.5)
        glow = QColor(accent)
        glow.setAlpha(150 if self._thinking else 90)
        painter.setBrush(glow)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(15, -0.5, 6, 5)
        painter.setBrush(accent)
        painter.drawEllipse(QPointF(18, 3.2), 1.8, 1.8)

        # 身体（画在头盔后面）
        painter.setBrush(QColor("#f4f6fa"))
        painter.setPen(QPen(stroke, 1))
        painter.drawRoundedRect(13, 28, 10, 6.5, 2, 2)
        painter.setPen(QPen(yellow, 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(15, 30, 6, 2.5)

        # 头盔圆球
        helmet_g = QLinearGradient(7, 6, 29, 29)
        helmet_g.setColorAt(0.0, QColor("#ffffff"))
        helmet_g.setColorAt(0.6, QColor("#e6ebf3"))
        helmet_g.setColorAt(1.0, QColor("#c2cad8"))
        painter.setBrush(helmet_g)
        painter.setPen(QPen(stroke, 1))
        painter.drawEllipse(QPointF(18, 17.5), 13, 13)

        # 侧耳灯
        painter.setBrush(yellow)
        painter.setPen(QPen(QColor("#c99e14"), 0.8))
        painter.drawEllipse(QPointF(5.5, 17.5), 2.5, 2.5)
        painter.drawEllipse(QPointF(30.5, 17.5), 2.5, 2.5)

        # 面罩：深色玻璃
        visor_g = QLinearGradient(10.5, 10, 25.5, 24)
        visor_g.setColorAt(0.0, QColor("#2b3550"))
        visor_g.setColorAt(1.0, QColor("#151b29"))
        painter.setBrush(visor_g)
        painter.setPen(QPen(QColor("#10141f"), 0.8))
        painter.drawRoundedRect(10.5, 10, 15, 14, 5, 5)

        # 玻璃反光带
        painter.setBrush(QColor(255, 255, 255, 30))
        painter.setPen(Qt.NoPen)
        st = QPainterPath()
        st.moveTo(13, 11)
        st.lineTo(15.2, 11)
        st.lineTo(11.8, 23)
        st.lineTo(10.9, 20)
        st.closeSubpath()
        painter.drawPath(st)

        # 眼睛：发光圆眼（眨眼 = 横线）
        eye_y = 15.5 if not self._thinking else 14.3
        core = QColor("#e8f4ff")
        for ex in (14.6, 21.4):
            halo = QColor(accent)
            halo.setAlpha(55)
            painter.setBrush(halo)
            painter.drawEllipse(QPointF(ex, eye_y), 2.4, 2.4)
        if self._blink > 0:
            painter.setPen(QPen(core, 1.2, Qt.SolidLine, Qt.RoundCap))
            painter.setBrush(Qt.NoBrush)
            painter.drawLine(13.2, eye_y, 16, eye_y)
            painter.drawLine(20, eye_y, 22.8, eye_y)
        else:
            painter.setBrush(core)
            painter.drawEllipse(QPointF(14.6, eye_y), 1.3, 2)
            painter.drawEllipse(QPointF(21.4, eye_y), 1.3, 2)
            painter.setBrush(QColor(255, 255, 255, 230))
            painter.drawEllipse(QPointF(14.2, eye_y - 0.9), 0.5, 0.5)
            painter.drawEllipse(QPointF(21, eye_y - 0.9), 0.5, 0.5)

        # 嘴：微笑弧（thinking = 小 o）
        painter.setPen(QPen(QColor("#dfe9ff"), 1, Qt.SolidLine, Qt.RoundCap))
        painter.setBrush(Qt.NoBrush)
        if self._thinking:
            painter.drawEllipse(QPointF(18, 21.5), 1, 1)
        else:
            painter.drawArc(16.8, 19.6, 2.4, 2, 200 * 16, 140 * 16)

        self._paint_particles(painter, accent)
