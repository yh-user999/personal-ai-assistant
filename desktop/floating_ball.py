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
from chat_workers import _HealthWorker, retire
from PySide6.QtCore import QPointF, Qt, QTimer
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
            retire(worker)  # wait 收尸后销毁（防 sizedFree 堆损坏崩溃）
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
        """硬核机械风头部：平顶 + 45° 切角的工业外壳轮廓（宽度收敛，给身体留比重）。"""
        p = QPainterPath()
        p.moveTo(24, 16)     # 顶边左端（平顶）
        p.lineTo(56, 16)
        p.lineTo(61, 21)     # 右上切角
        p.lineTo(61, 47)
        p.lineTo(56, 52)     # 右下切角
        p.lineTo(24, 52)
        p.lineTo(19, 47)
        p.lineTo(19, 21)
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

    # ── 班德金属风 · 硬核机械版（平顶切角外壳 + 内嵌小屏 + 散热格栅）──

    @staticmethod
    def _draw_hex(painter: QPainter, cx: float, cy: float, r: float,
                  fill: QLinearGradient, stroke: QColor) -> None:
        """六角螺栓（平边朝上），中心带压痕点。"""
        p = QPainterPath()
        for i in range(6):
            ang = math.radians(60 * i)
            px = cx + r * math.cos(ang)
            py = cy + r * math.sin(ang)
            if i == 0:
                p.moveTo(px, py)
            else:
                p.lineTo(px, py)
        p.closeSubpath()
        painter.setBrush(fill)
        painter.setPen(QPen(stroke, 1))
        painter.drawPath(p)
        painter.setBrush(QColor(0, 0, 0, 60))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPointF(cx, cy), r * 0.32, r * 0.32)

    def _paint_bender(self, painter: QPainter, accent: QColor) -> None:
        limb_color = QColor("#454c5a")
        stroke = QColor("#525a6b")
        dark = QColor("#12151c")      # 屏幕内芯
        recess = QColor("#232833")    # 内凹底座
        steel = self._shell_gradient(19, 16, 42, 36)

        # 腿（先画，被身体压住根部）
        l_leg, r_leg = leg_angles(self._phase, self._dragging)
        for hip_x, ang in ((34, l_leg), (46, r_leg)):
            fx, fy = self._draw_limb(painter, hip_x, 64, 12, ang, 3.5, limb_color)
            painter.setPen(Qt.NoPen)
            painter.setBrush(limb_color)
            painter.drawEllipse(int(fx - 2.5), int(fy - 1.5), 5, 4)

        # 手臂
        waving = self._wave >= 0
        for side, sh_x in (("L", 29), ("R", 51)):
            ang = arm_angle(self.state, self._phase, self._dragging, waving, self._wave, side)
            hx, hy = self._draw_limb(painter, sh_x, 56, 15, ang, 3.5, limb_color)
            painter.setPen(Qt.NoPen)
            painter.setBrush(limb_color)
            painter.drawEllipse(int(hx - 2.5), int(hy - 2.5), 5, 5)

        # 侧面六角螺栓（机械感侧耳）
        for bx in (13.5, 66.5):
            self._draw_hex(painter, bx, 34, 6, steel, stroke)

        # 颈部（方钢）
        painter.setBrush(QColor("#2e3440"))
        painter.setPen(Qt.NoPen)
        painter.drawRect(34, 50, 12, 6)

        # 身体（方直外壳 + 横向拼缝）
        painter.setBrush(self._shell_gradient(27, 54, 26, 13))
        painter.setPen(QPen(stroke, 1))
        painter.drawRoundedRect(27, 54, 26, 13, 2, 2)
        painter.setPen(QPen(QColor(0, 0, 0, 70), 1))
        painter.drawLine(28, 61, 52, 61)

        # 胸口检修屏 + 状态脉冲
        painter.setBrush(recess)
        painter.setPen(Qt.NoPen)
        painter.drawRect(35, 56, 10, 6)
        pulse = QColor(accent)
        pulse.setAlpha(int(140 + 100 * math.sin(self._phase * 2)))
        painter.setBrush(pulse)
        painter.drawEllipse(39, 58, 3, 3)

        # 天线（方形基座 + 状态光点）
        painter.setPen(QPen(stroke, 2))
        painter.drawLine(40, 16, 40, 8)
        glow = QColor(accent)
        glow.setAlpha(int(70 + 70 * math.sin(self._phase * 2)))
        painter.setBrush(glow)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(36, 1, 8, 8)
        painter.setBrush(accent)
        painter.drawRect(38, 3, 4, 4)

        # 头（平顶切角外壳）
        painter.setBrush(steel)
        painter.setPen(QPen(stroke, 1.2))
        painter.drawPath(self._head_path())

        # 顶板拼缝 + 铆钉 + 顶缘高光
        painter.setPen(QPen(QColor(0, 0, 0, 80), 1))
        painter.drawLine(22, 22, 58, 22)
        painter.setBrush(QColor("#2e3440"))
        painter.drawEllipse(QPointF(24.5, 19), 1.2, 1.2)
        painter.drawEllipse(QPointF(55.5, 19), 1.2, 1.2)
        painter.setPen(QPen(QColor(255, 255, 255, 46), 1.5, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(26, 17.5, 54, 17.5)

        # 内嵌显示屏（只占头宽 1/3——脸不再喧宾夺主）
        painter.setBrush(recess)
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(26, 26, 28, 11, 2, 2)
        painter.setBrush(dark)
        painter.drawRoundedRect(27, 27, 26, 9, 1, 1)

        # LED 扫描眼（分段 LED 组；眨眼 = 高度压扁；thinking = 加辉光）
        eye_h = 4.0 * (1.0 - 0.85 * abs(math.sin(self._blink * math.pi)))
        eye_h = max(1.6, eye_h)
        eye_y = 31.5 - eye_h / 2
        if self.state == "thinking":
            halo = QColor(accent)
            halo.setAlpha(60)
            painter.setBrush(halo)
            painter.drawRoundedRect(28, 28, 24, 7, 2, 2)
        painter.setBrush(QColor(accent))
        painter.drawRoundedRect(29, eye_y, 22, eye_h, 1, 1)
        painter.setPen(QPen(QColor(0, 0, 0, 130), 1))
        painter.drawLine(36, eye_y + 0.8, 36, eye_y + eye_h - 0.8)
        painter.drawLine(44, eye_y + 0.8, 44, eye_y + eye_h - 0.8)

        # 下颚散热格栅（竖栅）
        painter.setBrush(recess)
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(32, 40, 16, 7, 1, 1)
        painter.setPen(QPen(QColor("#565e70"), 1.4))
        for gx in (34.5, 37.5, 40.5, 43.5, 46.5):
            painter.drawLine(gx, 41, gx, 46)

        self._paint_particles(painter, accent)

    # ── 白色宇航员风 · 重制版（圆球头盔 + 深色玻璃面罩 + 发光圆眼）──

    def _paint_astro(self, painter: QPainter, accent: QColor) -> None:
        stroke = QColor("#9aa5b8")
        yellow = QColor("#f5c518")
        yellow_dark = QColor("#c99e14")

        # 腿：白色短腿 + 黄色靴子（荡腿 = 左右微摆）
        l_leg, r_leg = leg_angles(self._phase, self._dragging)
        for cx_leg, ang in ((33, l_leg), (47, r_leg)):
            dx = int(ang * 0.25)
            painter.setBrush(QColor("#e2e7ef"))
            painter.setPen(QPen(stroke, 1))
            painter.drawRoundedRect(cx_leg - 3.5 + dx, 62, 7, 12, 3, 3)
            painter.setBrush(yellow)
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(cx_leg - 4.5 + dx, 72, 9, 4.5, 2, 2)

        # 手臂：白管臂 + 圆手套（招手/托下巴/上举姿势复用）
        waving = self._wave >= 0
        for side, sh_x in (("L", 26), ("R", 54)):
            ang = arm_angle(self.state, self._phase, self._dragging, waving, self._wave, side)
            hx, hy = self._draw_limb(painter, sh_x, 55, 14, ang, 5, QColor("#dfe4ec"))
            painter.setPen(QPen(stroke, 1))
            painter.setBrush(QColor("#ffffff"))
            painter.drawEllipse(QPointF(hx, hy), 4.5, 4.5)

        # 身体：白宇航服 + 黄色舷窗描边 + 状态核心灯
        painter.setBrush(QColor("#f4f6fa"))
        painter.setPen(QPen(stroke, 1))
        painter.drawRoundedRect(30, 52, 20, 13, 5, 5)
        painter.setPen(QPen(yellow, 1.4))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(33, 55, 14, 7, 3, 3)
        pulse = QColor(accent)
        pulse.setAlpha(int(150 + 90 * math.sin(self._phase * 2)))
        painter.setBrush(pulse)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPointF(40, 58.5), 2, 2)

        # 天线（小月签名）
        painter.setPen(QPen(stroke, 2))
        painter.drawLine(40, 11, 40, 5)
        glow = QColor(accent)
        glow.setAlpha(int(70 + 70 * math.sin(self._phase * 2)))
        painter.setBrush(glow)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(36.5, 0.5, 7, 7)
        painter.setBrush(accent)
        painter.drawEllipse(QPointF(40, 4), 2.5, 2.5)

        # 头盔：正圆球体 + 底部体积阴影 + 顶部高光
        helmet_g = QLinearGradient(22, 12, 58, 50)
        helmet_g.setColorAt(0.0, QColor("#ffffff"))
        helmet_g.setColorAt(0.6, QColor("#e6ebf3"))
        helmet_g.setColorAt(1.0, QColor("#c2cad8"))
        painter.setBrush(helmet_g)
        painter.setPen(QPen(stroke, 1.2))
        painter.drawEllipse(QPointF(40, 31), 21, 21)
        painter.setPen(QPen(QColor(70, 80, 100, 60), 3, Qt.SolidLine, Qt.RoundCap))
        painter.setBrush(Qt.NoBrush)
        painter.drawArc(23, 14, 34, 34, 30 * 16, 120 * 16)
        painter.setPen(QPen(QColor(255, 255, 255, 200), 3, Qt.SolidLine, Qt.RoundCap))
        painter.drawArc(25, 16, 30, 26, 170 * 16, 100 * 16)

        # 侧耳灯（黄色圆灯）
        painter.setBrush(yellow)
        painter.setPen(QPen(yellow_dark, 1))
        painter.drawEllipse(QPointF(19, 31), 4.5, 4.5)
        painter.drawEllipse(QPointF(61, 31), 4.5, 4.5)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#ffefb8"))
        painter.drawEllipse(QPointF(18, 29.8), 1.5, 1.5)
        painter.drawEllipse(QPointF(60, 29.8), 1.5, 1.5)

        # 面罩：深色玻璃航太镜片 + 顶缘内高光
        visor_g = QLinearGradient(28, 21, 52, 42)
        visor_g.setColorAt(0.0, QColor("#2b3550"))
        visor_g.setColorAt(1.0, QColor("#151b29"))
        painter.setBrush(visor_g)
        painter.setPen(QPen(QColor("#10141f"), 1))
        painter.drawRoundedRect(28, 21, 24, 21, 8, 8)
        painter.setPen(QPen(QColor(255, 255, 255, 36), 1.2, Qt.SolidLine, Qt.RoundCap))
        painter.setBrush(Qt.NoBrush)
        painter.drawLine(31, 23.5, 49, 23.5)

        # 玻璃斜向反光带
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 30))
        streak = QPainterPath()
        streak.moveTo(31.5, 22)
        streak.lineTo(35, 22)
        streak.lineTo(30, 40)
        streak.lineTo(28, 36)
        streak.closeSubpath()
        painter.drawPath(streak)
        painter.setBrush(QColor(255, 255, 255, 16))
        streak2 = QPainterPath()
        streak2.moveTo(38, 22)
        streak2.lineTo(40, 22)
        streak2.lineTo(34, 41)
        streak2.lineTo(32, 41)
        streak2.closeSubpath()
        painter.drawPath(streak2)

        # 眼睛：玻璃后的发光圆眼（眨眼 = 横线；error = 灰线）
        eye_y = 30.0 if self.state != "thinking" else 28.0
        eye_core = QColor("#e8f4ff") if self.state != "error" else QColor("#8b93a3")
        for ex in (35.5, 44.5):
            halo = QColor(accent)
            halo.setAlpha(55)
            painter.setBrush(halo)
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QPointF(ex, eye_y), 4, 4)
        if self._blink > 0 or self.state == "error":
            painter.setPen(QPen(eye_core, 1.8, Qt.SolidLine, Qt.RoundCap))
            painter.setBrush(Qt.NoBrush)
            painter.drawLine(33, int(eye_y), 38, int(eye_y))
            painter.drawLine(42, int(eye_y), 47, int(eye_y))
        else:
            painter.setBrush(eye_core)
            painter.drawEllipse(QPointF(35.5, eye_y), 2.2, 3.4)
            painter.drawEllipse(QPointF(44.5, eye_y), 2.2, 3.4)
            painter.setBrush(QColor(255, 255, 255, 230))
            painter.drawEllipse(QPointF(34.8, eye_y - 1.4), 0.9, 0.9)
            painter.drawEllipse(QPointF(43.8, eye_y - 1.4), 0.9, 0.9)
        if self.state == "thinking" and self._blink == 0:
            # 四角星闪点（thinking 专属小星星）
            painter.setBrush(QColor(255, 255, 255, 220))
            sx, sy, r1 = 48.5, 25.5, 2.2
            star = QPainterPath()
            star.moveTo(sx, sy - r1)
            star.quadTo(sx, sy, sx + r1, sy)
            star.quadTo(sx, sy, sx, sy + r1)
            star.quadTo(sx, sy, sx - r1, sy)
            star.quadTo(sx, sy, sx, sy - r1)
            painter.drawPath(star)

        # 嘴：微笑弧（thinking = 圆圆的 o；error = 嘴角向下）
        painter.setPen(QPen(QColor("#dfe9ff"), 1.3, Qt.SolidLine, Qt.RoundCap))
        painter.setBrush(Qt.NoBrush)
        if self.state == "thinking":
            painter.drawEllipse(QPointF(40, 36.5), 1.6, 1.6)
        elif self.state == "error":
            painter.drawArc(37, 33, 6, 5, 20 * 16, 140 * 16)
        else:
            painter.drawArc(37, 34, 6, 4, 200 * 16, 140 * 16)

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
        x = pos.x() - self.panel.width() + self.W
        y = pos.y() - self.panel.height() + self.H
        # 面板可被拉大：锚定后夹回屏幕可视区，避免跑出屏幕外找不回
        geo = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        x = max(geo.left() + 8, min(x, geo.right() - self.panel.width() - 8))
        y = max(geo.top() + 8, min(y, geo.bottom() - self.panel.height() - 8))
        self.panel.move(x, y)
