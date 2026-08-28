"""悬浮机器人：无边框透明窗口，可拖拽，点击展开聊天面板。

形象：QPainter 自绘机器人，双皮肤（右键菜单切换，QSettings 持久化）——
- bender：班德金属风（金属灰桶形头 + 视窗单眼 + 格栅嘴）
- astro：白色宇航员风（原创致敬：白盔 + 琥珀面罩 + 黑圆眼 + 黄色点缀）
动作：思考=右手托下巴、被拖拽=双臂上举+荡腿、双击=招手、呼吸摆动。
姿态计算在 robot_pose.py（纯数学，可单测），本文件只负责画。
"""
import math
import random
from pathlib import Path

from chat_panel import ChatPanel
from chat_workers import _HealthWorker
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QMenu, QWidget
from robot_pose import arm_angle, leg_angles
from skins import SKIN_NAMES, current_skin, set_skin

# 状态 → 主色（光晕/天线/眼睛/胸口屏联动）
STATE_COLORS = {
    "idle": "#60a5fa",      # 蓝
    "online": "#34d399",    # 绿
    "thinking": "#fbbf24",  # 琥珀
    "error": "#f87171",     # 红
}

ASSET_SVG = Path(__file__).resolve().parent / "assets" / "robot.svg"


class FloatingBall(QWidget):
    W, H = 80, 84  # 加高加宽：给手脚留空间

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(self.W, self.H)
        self._drag_offset = None   # 按下时鼠标相对窗口的偏移
        self._moved = False        # 本次按下是否发生了拖拽
        self.panel: ChatPanel | None = None
        self.state = "idle"
        self.skin = current_skin()  # bender / astro（右键换肤，持久化）

        # 动画状态
        self._phase = 0.0            # 呼吸相位
        self._blink = 0.0            # 眨眼进度 0..1
        self._blink_cd = random.uniform(3.0, 5.5)  # 距下次眨眼秒数
        self._wave = -1.0            # 招手进度 0..1（-1 = 未在招手）
        self._dragging = False       # 是否正在被拖拽

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)        # 20fps 足够

        # 窗口丢失自愈：Windows 在 Explorer 重启/睡眠唤醒/全屏切换后可能
        # 让无边框半透明小窗消失而进程存活——每 10s 自查，丢了就自动恢复
        self._restore_timer = QTimer(self)
        self._restore_timer.timeout.connect(self._ensure_visible)
        self._restore_timer.start(10_000)

        # 断线检测：每 60s 后台 ping 服务器，失败亮红灯
        from api_client import ApiClient

        self._health_client = ApiClient()
        self._health_worker = None
        self._health_timer = QTimer(self)
        self._health_timer.timeout.connect(self._check_health)
        self._health_timer.start(60_000)
        self._check_health()

        # 默认位置：屏幕右下角
        screen = self.screen() or QApplication.primaryScreen()
        geo = screen.availableGeometry()
        self.move(geo.right() - self.W - 40, geo.bottom() - self.H - 40)

    # ── 对外接口 ───────────────────────────────────────────

    def set_state(self, state: str) -> None:
        """切换状态指示灯颜色：idle/online/thinking/error。"""
        if state in STATE_COLORS:
            self.state = state
            self.update()

    def wave(self) -> None:
        """招手问好一次（约 1.2s）。"""
        self._wave = 0.0
        self.update()

    def _ensure_visible(self) -> None:
        """窗口丢失自愈：进程活着但窗口不见时自动恢复显示（10s 内自愈）。"""
        if not self.isVisible():
            self.show()
            self.raise_()

    # ── 健康检查 ───────────────────────────────────────────

    def _check_health(self) -> None:
        if self._health_worker is not None:
            return  # 上一次还没跑完，跳过本轮
        self._health_worker = _HealthWorker(self._health_client)
        self._health_worker.result.connect(self._on_health)
        self._health_worker.start()

    def _on_health(self, ok: bool) -> None:
        worker = self._health_worker
        self._health_worker = None
        if worker:
            worker.deleteLater()  # 防 QThread 慢性泄漏
        if self.state == "thinking":
            return  # 聊天中不打断状态
        self.set_state("online" if ok else "error")

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
        # 招手进度
        if self._wave >= 0:
            self._wave += 0.08
            if self._wave > 1.0:
                self._wave = -1.0
        self.update()

    # ── 绘制 ───────────────────────────────────────────────

    def _draw_limb(
        self,
        painter: QPainter,
        x: float,
        y: float,
        length: float,
        angle_deg: float,
        width: float,
        color: QColor,
    ) -> None:
        """画一条圆头肢体：从 (x,y) 出发，角度相对垂直向下（正=顺时针）。"""
        rad = math.radians(angle_deg)
        ex = x + length * math.sin(rad)
        ey = y + length * math.cos(rad)
        pen = QPen(color, width)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.drawLine(int(x), int(y), int(ex), int(ey))
        return ex, ey

    def _state_color(self) -> QColor:
        return QColor(STATE_COLORS.get(self.state, STATE_COLORS["idle"]))

    @staticmethod
    def _shell_gradient(x: float, y: float, w: float, h: float) -> QLinearGradient:
        """机身渐变：金属灰——左上亮 → 右下暗（班德式机械质感）。"""
        g = QLinearGradient(x, y, x + w, y + h)
        g.setColorAt(0.0, QColor("#a9b2c4"))
        g.setColorAt(0.45, QColor("#6b7488"))
        g.setColorAt(1.0, QColor("#3a414e"))
        return g

    @staticmethod
    def _head_path() -> QPainterPath:
        """班德式头部：圆顶窄顶 + 底部外扩（桶形）。"""
        p = QPainterPath()
        p.moveTo(18, 34)
        p.quadTo(18, 14, 40, 14)   # 左侧弧 → 顶中点
        p.quadTo(62, 14, 62, 34)   # 顶中点 → 右侧弧
        p.lineTo(66, 48)           # 右侧外扩
        p.quadTo(66, 52, 60, 52)
        p.lineTo(20, 52)
        p.quadTo(14, 52, 14, 48)
        p.lineTo(18, 34)
        p.closeSubpath()
        return p

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # 呼吸浮动：围绕中心缩放
        scale = 1 + 0.03 * math.sin(self._phase)
        cx, cy = self.W / 2, self.H / 2
        painter.translate(cx, cy)
        painter.scale(scale, scale)
        painter.translate(-cx, -cy)

        accent = self._state_color()

        # ── 地面阴影（悬浮感，两套皮肤共享）──
        shadow = QColor(0, 0, 0, 42)
        painter.setBrush(shadow)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(18, 77, 44, 5)

        # ── 状态色光晕（若隐若现，随呼吸）──
        halo = QColor(accent)
        halo.setAlpha(int(26 + 18 * math.sin(self._phase)))
        painter.setBrush(halo)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(10, 12, self.W - 20, self.W - 20)

        if self.skin == "astro":
            self._paint_astro(painter, accent)
        elif self.skin == "classic":
            self._paint_classic(painter, accent)
        else:
            self._paint_bender(painter, accent)

    # ── 班德金属风（金属灰桶形头 + 视窗单眼 + 格栅嘴）──

    def _paint_bender(self, painter: QPainter, accent: QColor) -> None:
        limb_color = QColor("#4a5160")
        stroke = QColor("#565e70")

        # 腿（先画，被身体压住根部）
        l_leg, r_leg = leg_angles(self._phase, self._dragging)
        for hip_x, ang in ((34, l_leg), (46, r_leg)):
            fx, fy = self._draw_limb(painter, hip_x, 62, 13, ang, 3.5, limb_color)
            painter.setPen(Qt.NoPen)
            painter.setBrush(limb_color)
            painter.drawEllipse(int(fx - 2.5), int(fy - 1.5), 5, 4)

        # 手臂
        waving = self._wave >= 0
        for side, sh_x in (("L", 28), ("R", 52)):
            ang = arm_angle(self.state, self._phase, self._dragging, waving, self._wave, side)
            hx, hy = self._draw_limb(painter, sh_x, 53, 16, ang, 3.5, limb_color)
            painter.setPen(Qt.NoPen)
            painter.setBrush(limb_color)
            painter.drawEllipse(int(hx - 2.5), int(hy - 2.5), 5, 5)

        # 耳朵（金属侧板）
        painter.setBrush(self._shell_gradient(5, 28, 9, 14))
        painter.setPen(QPen(stroke, 1))
        painter.drawRoundedRect(6, 29, 8, 14, 4, 4)
        painter.drawRoundedRect(66, 29, 8, 14, 4, 4)

        # 身体（金属渐变）
        painter.setBrush(self._shell_gradient(26, 50, 28, 14))
        painter.setPen(QPen(stroke, 1))
        painter.drawRoundedRect(26, 50, 28, 14, 6, 6)

        # 胸口小屏 + 状态色脉冲点
        painter.setBrush(QColor("#14161c"))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(35, 55, 10, 5, 2, 2)
        pulse = QColor(accent)
        pulse.setAlpha(int(140 + 100 * math.sin(self._phase * 2)))
        painter.setBrush(pulse)
        painter.drawEllipse(39, 56, 3, 3)

        # 天线（脉冲光点）
        painter.setPen(QPen(QColor("#565e70"), 2))
        painter.drawLine(40, 14, 40, 7)
        glow = QColor(accent)
        glow.setAlpha(int(70 + 70 * math.sin(self._phase * 2)))
        painter.setBrush(glow)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(35, 0, 10, 10)
        painter.setBrush(accent)
        painter.drawEllipse(37, 2, 6, 6)

        # 桶形头
        painter.setBrush(self._shell_gradient(14, 14, 52, 38))
        painter.setPen(QPen(stroke, 1.2))
        painter.drawPath(self._head_path())

        # 顶部高光弧
        painter.setPen(QPen(QColor(255, 255, 255, 40), 3, Qt.SolidLine, Qt.RoundCap))
        painter.drawArc(26, 17, 28, 14, 180 * 16, 180 * 16)

        # 视窗单眼（眨眼 = 高度压扁）
        visor_h = 10.0 * (1.0 - 0.85 * abs(math.sin(self._blink * math.pi)))
        outer = QColor(accent)
        outer.setAlpha(50)
        painter.setBrush(outer)
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(20, 28 - 3, 40, 16, 8, 8)
        painter.setBrush(QColor("#20242c"))
        painter.drawRoundedRect(24, int(32 - visor_h / 2) + 1, 32, int(visor_h) - 2, 5, 5)
        painter.setBrush(QColor(accent))
        painter.drawRoundedRect(25, int(32 - visor_h / 2) + 2, 30, max(2, int(visor_h)) - 4, 4, 4)

        # 格栅嘴（3 条横槽）
        painter.setPen(QPen(QColor("#565e70"), 1.6, Qt.SolidLine, Qt.RoundCap))
        for gy in (43.5, 46.0, 48.5):
            painter.drawLine(28, int(gy), 52, int(gy))

        self._paint_particles(painter, accent)

    # ── 白色宇航员风（原创致敬：白盔 + 琥珀面罩 + 黑圆眼 + 黄色点缀）──

    def _paint_astro(self, painter: QPainter, accent: QColor) -> None:
        white_g = QLinearGradient(20, 10, 60, 52)
        white_g.setColorAt(0.0, QColor("#ffffff"))
        white_g.setColorAt(0.55, QColor("#dfe4ec"))
        white_g.setColorAt(1.0, QColor("#b9c1ce"))
        stroke = QColor("#9aa3b5")
        yellow = QColor("#f5c518")

        # 腿：白色粗短腿 + 黄色脚块（荡腿 = 左右微摆）
        l_leg, r_leg = leg_angles(self._phase, self._dragging)
        for cx_leg, ang in ((32, l_leg), (48, r_leg)):
            dx = int(ang * 0.25)
            painter.setBrush(white_g)
            painter.setPen(QPen(stroke, 1))
            painter.drawRoundedRect(cx_leg - 4 + dx, 60, 8, 15, 4, 4)
            painter.setBrush(yellow)
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(cx_leg - 5 + dx, 73, 10, 4, 2, 2)

        # 手臂：白色粗管臂 + 圆手（招手/托下巴/上举姿势复用）
        waving = self._wave >= 0
        for side, sh_x in (("L", 24), ("R", 56)):
            ang = arm_angle(self.state, self._phase, self._dragging, waving, self._wave, side)
            hx, hy = self._draw_limb(painter, sh_x, 55, 15, ang, 6, QColor("#dfe4ec"))
            painter.setPen(QPen(stroke, 1))
            painter.setBrush(QColor("#ffffff"))
            painter.drawEllipse(int(hx - 5), int(hy - 5), 10, 10)

        # 身体：白色小身板 + 黄色描边 + 状态色核心灯
        painter.setBrush(white_g)
        painter.setPen(QPen(stroke, 1))
        painter.drawRoundedRect(30, 51, 20, 13, 6, 6)
        painter.setPen(QPen(yellow, 1.4))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(33, 54, 14, 7, 3, 3)
        pulse = QColor(accent)
        pulse.setAlpha(int(150 + 90 * math.sin(self._phase * 2)))
        painter.setBrush(pulse)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(38, 56, 4, 4)

        # 天线（小月签名）
        painter.setPen(QPen(QColor("#9aa3b5"), 2))
        painter.drawLine(40, 10, 40, 5)
        glow = QColor(accent)
        glow.setAlpha(int(70 + 70 * math.sin(self._phase * 2)))
        painter.setBrush(glow)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(36, 0, 8, 8)
        painter.setBrush(accent)
        painter.drawEllipse(37.5, 1.5, 5, 5)

        # 头：白色大圆盔 + 两侧小圆耳 + 顶部高光
        painter.setBrush(white_g)
        painter.setPen(QPen(stroke, 1.2))
        painter.drawRoundedRect(19, 9, 42, 42, 20, 20)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#dfe4ec"))
        painter.drawEllipse(14, 27, 10, 10)
        painter.drawEllipse(56, 27, 10, 10)
        painter.setPen(QPen(QColor(255, 255, 255, 200), 4, Qt.SolidLine, Qt.RoundCap))
        painter.drawArc(27, 14, 26, 16, 200 * 16, 140 * 16)

        # 面罩：琥珀圆角屏（error 变灰；thinking 发光）
        face = QColor("#f2b33d") if self.state != "error" else QColor("#8b93a3")
        if self.state == "thinking":
            fg = QColor(accent)
            fg.setAlpha(70)
            painter.setBrush(fg)
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(24, 17, 32, 28, 8, 8)
        painter.setBrush(face)
        painter.setPen(QPen(QColor("#b8822a") if self.state != "error" else QColor("#6b7280"), 1))
        painter.drawRoundedRect(27, 20, 26, 23, 7, 7)

        # 眼睛：黑圆眼 + 高光（眨眼 = 闭眼横线；思考 = 上翻；断线 = 横线）
        eye_y = 28 if self.state != "thinking" else 25
        painter.setBrush(QColor("#1a1d24"))
        painter.setPen(Qt.NoPen)
        if self._blink > 0 or self.state == "error":
            painter.setPen(QPen(QColor("#1a1d24"), 2, Qt.SolidLine, Qt.RoundCap))
            painter.setBrush(Qt.NoBrush)
            painter.drawLine(32, eye_y, 38, eye_y)
            painter.drawLine(42, eye_y, 48, eye_y)
        else:
            painter.drawEllipse(32, eye_y, 6, 6)
            painter.drawEllipse(42, eye_y, 6, 6)
            painter.setBrush(QColor(255, 255, 255, 210))
            painter.drawEllipse(34, eye_y + 1, 2, 2)
            painter.drawEllipse(44, eye_y + 1, 2, 2)

        # 嘴巴：短横线（thinking 时 O 型）
        painter.setPen(QPen(QColor("#8a5a14"), 1.6, Qt.SolidLine, Qt.RoundCap))
        if self.state == "thinking":
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(38, 37, 4, 4)
        else:
            painter.drawLine(37, 39, 43, 39)

        self._paint_particles(painter, accent)

    def _paint_particles(self, painter: QPainter, accent: QColor) -> None:
        """思考时环绕粒子（3 个小光点绕头转，三套皮肤共享）。"""
        if self.state != "thinking":
            return
        painter.setPen(Qt.NoPen)
        for i in range(3):
            ang = self._phase * 1.8 + i * 2.094
            px = 40 + 16 * math.cos(ang)
            py = 33 + 16 * math.sin(ang)
            a = int(110 + 90 * math.sin(self._phase * 3 + i))
            dot = QColor(accent)
            dot.setAlpha(max(0, a))
            painter.setBrush(dot)
            painter.drawEllipse(int(px), int(py), 3, 3)

    # ── 原版萌系风（暗色圆角头 + 双 LED 大眼 + 微笑/腮红 + 胸口屏）──

    def _paint_classic(self, painter: QPainter, accent: QColor) -> None:
        limb_color = QColor("#3f4654")
        stroke = QColor("#4a5264")
        dark_g = QLinearGradient(14, 14, 62, 52)
        dark_g.setColorAt(0.0, QColor("#4a5266"))
        dark_g.setColorAt(0.45, QColor("#2c313d"))
        dark_g.setColorAt(1.0, QColor("#1a1d24"))

        # 腿
        l_leg, r_leg = leg_angles(self._phase, self._dragging)
        for hip_x, ang in ((34, l_leg), (46, r_leg)):
            fx, fy = self._draw_limb(painter, hip_x, 62, 13, ang, 3.5, limb_color)
            painter.setPen(Qt.NoPen)
            painter.setBrush(limb_color)
            painter.drawEllipse(int(fx - 2.5), int(fy - 1.5), 5, 4)

        # 手臂
        waving = self._wave >= 0
        for side, sh_x in (("L", 28), ("R", 52)):
            ang = arm_angle(self.state, self._phase, self._dragging, waving, self._wave, side)
            hx, hy = self._draw_limb(painter, sh_x, 53, 16, ang, 3.5, limb_color)
            painter.setPen(Qt.NoPen)
            painter.setBrush(limb_color)
            painter.drawEllipse(int(hx - 2.5), int(hy - 2.5), 5, 5)

        # 耳朵（头部两侧小侧板）
        painter.setBrush(dark_g)
        painter.setPen(QPen(stroke, 1))
        painter.drawRoundedRect(11, 26, 8, 13, 4, 4)
        painter.drawRoundedRect(61, 26, 8, 13, 4, 4)

        # 身体
        painter.setBrush(dark_g)
        painter.setPen(QPen(stroke, 1))
        painter.drawRoundedRect(28, 50, 24, 14, 7, 7)

        # 胸口小屏 + 状态色脉冲点
        painter.setBrush(QColor("#14161c"))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(35, 55, 10, 5, 2, 2)
        pulse = QColor(accent)
        pulse.setAlpha(int(140 + 100 * math.sin(self._phase * 2)))
        painter.setBrush(pulse)
        painter.drawEllipse(39, 56, 3, 3)

        # 天线（脉冲光点）
        painter.setPen(QPen(QColor("#4b5563"), 2))
        painter.drawLine(40, 14, 40, 7)
        glow = QColor(accent)
        glow.setAlpha(int(70 + 70 * math.sin(self._phase * 2)))
        painter.setBrush(glow)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(35, 0, 10, 10)
        painter.setBrush(accent)
        painter.drawEllipse(37, 2, 6, 6)

        # 头部（暗色圆角）
        painter.setBrush(dark_g)
        painter.setPen(QPen(stroke, 1))
        painter.drawRoundedRect(18, 14, 44, 38, 12, 12)

        # 顶部高光弧
        painter.setPen(QPen(QColor(255, 255, 255, 34), 3, Qt.SolidLine, Qt.RoundCap))
        painter.drawArc(24, 18, 20, 12, 180 * 16, 180 * 16)

        # 双 LED 大眼（光晕 + 高光；眨眼 = 高度压扁）
        eye_h = 11.0 * (1.0 - 0.85 * abs(math.sin(self._blink * math.pi)))
        eye_w = 11.0
        outer = QColor(accent)
        outer.setAlpha(55)
        painter.setBrush(outer)
        painter.setPen(Qt.NoPen)
        for ex in (26, 43):
            painter.drawEllipse(int(ex - 3), int(24 + (11 - eye_h) / 2), int(eye_w + 6), int(eye_h + 6))
        painter.setBrush(QColor(accent))
        for ex in (26, 43):
            painter.drawEllipse(int(ex), int(27 + (11 - eye_h) / 2), int(eye_w), max(2, int(eye_h)))
        if self._blink == 0:
            painter.setBrush(QColor(255, 255, 255, 170))
            for ex in (29, 46):
                painter.drawEllipse(ex, 29, 3, 3)

        # 腮红（error 时消失）
        if self.state != "error":
            painter.setBrush(QColor(255, 122, 156, 74))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(22, 40, 6, 3.5)
            painter.drawEllipse(52, 40, 6, 3.5)

        # 嘴巴（微笑 / 思考圆 / 难过）
        painter.setPen(QPen(QColor("#9aa3b5"), 2, Qt.SolidLine, Qt.RoundCap))
        painter.setBrush(Qt.NoBrush)
        if self.state == "thinking":
            painter.drawEllipse(37, 42, 6, 6)
        elif self.state == "error":
            painter.drawArc(34, 40, 12, 9, 20 * 16, 140 * 16)  # 嘴角向下
        else:
            painter.drawArc(34, 39, 12, 9, 200 * 16, 140 * 16)

        self._paint_particles(painter, accent)

    # ── 交互 ───────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            # 记录按下时鼠标相对窗口左上角的偏移 + 重置移动标记
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._moved = False
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and (event.buttons() & Qt.LeftButton):
            new_pos = event.globalPosition().toPoint() - self._drag_offset
            if (new_pos - self.pos()).manhattanLength() > 3:
                self._moved = True  # 位移超过 3px 判定为拖拽
                self._dragging = True
            self.move(new_pos)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            if not self._moved:
                # 单击：不弹面板，只快速眨眼一次作为反馈
                self._blink = 0.01
            self._drag_offset = None
            self._moved = False
            self._dragging = False
            event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        """双击 → 打开/收起聊天面板，并招手问好。"""
        if event.button() == Qt.LeftButton:
            self.toggle_panel()
            self.wave()
            event.accept()

    def contextMenuEvent(self, event) -> None:
        """右键菜单：打开面板 / 换肤 / 退出。"""
        menu = QMenu(self)
        menu.addAction("打开/收起面板", self.toggle_panel)
        skin_menu = menu.addMenu("换肤")
        for name, label in SKIN_NAMES.items():
            act = skin_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(self.skin == name)
            act.triggered.connect(lambda checked=False, n=name: self._switch_skin(n))
        menu.addSeparator()
        menu.addAction("退出", QApplication.quit)
        menu.exec(event.globalPos())

    def _switch_skin(self, name: str) -> None:
        """切换皮肤：更新绘制 + 持久化 + 同步刷新托盘图标。"""
        if name not in SKIN_NAMES or name == self.skin:
            return
        self.skin = name
        set_skin(name)
        self.update()
        tray = getattr(self, "_tray", None)
        if tray is not None:
            tray.refresh_icon()

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
        self.panel.move(pos.x() - self.panel.width() + self.W, pos.y() - self.panel.height() + self.H)
