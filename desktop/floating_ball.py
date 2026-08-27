"""悬浮机器人：无边框透明窗口，可拖拽，点击展开聊天面板。

形象：QPainter 自绘机器人（天线 + 圆角头 + LED 眼睛 + 状态指示灯）。
第 14 课新增：手脚 + 动作——
- 手臂/腿随状态切换姿势：思考=右手托下巴、被拖拽=双臂上举+荡腿、
  双击开面板=招手问好、平时随呼吸轻微摆动
- 姿态计算在 robot_pose.py（纯数学，可单测），本文件只负责画
换肤：把 SVG 放到 desktop/assets/robot.svg 会自动替换为素材渲染（v2 扩展位）。
"""
import math
import random
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QMenu, QWidget

from chat_panel import ChatPanel
from robot_pose import arm_angle, leg_angles

# 状态 → 指示灯颜色（可扩展：thinking 时联动聊天）
STATE_COLORS = {
    "idle": "#60a5fa",      # 蓝
    "online": "#34d399",    # 绿
    "thinking": "#fbbf24",  # 琥珀
    "error": "#f87171",     # 红
}

ASSET_SVG = Path(__file__).resolve().parent / "assets" / "robot.svg"


class _HealthWorker(QThread):
    """后台健康检查线程：断线时机器人亮红灯。"""
    result = Signal(bool)

    def __init__(self, client) -> None:
        super().__init__()
        self.client = client

    def run(self) -> None:
        self.result.emit(self.client.health())


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

        # 动画状态
        self._phase = 0.0            # 呼吸相位
        self._blink = 0.0            # 眨眼进度 0..1
        self._blink_cd = random.uniform(3.0, 5.5)  # 距下次眨眼秒数
        self._wave = -1.0            # 招手进度 0..1（-1 = 未在招手）
        self._dragging = False       # 是否正在被拖拽

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)        # 20fps 足够

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

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # 呼吸浮动：围绕中心缩放
        scale = 1 + 0.03 * math.sin(self._phase)
        cx, cy = self.W / 2, self.H / 2
        painter.translate(cx, cy)
        painter.scale(scale, scale)
        painter.translate(-cx, -cy)

        # 光晕底（若隐若现，随呼吸）
        halo = QColor("#2b5cff")
        halo.setAlpha(int(28 + 18 * math.sin(self._phase)))
        painter.setBrush(halo)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(10, 12, self.W - 20, self.W - 20)

        limb_color = QColor("#3a3f4b")

        # ── 腿（先画，被身体压住根部）──
        l_leg, r_leg = leg_angles(self._phase, self._dragging)
        for hip_x, ang in ((34, l_leg), (46, r_leg)):
            fx, fy = self._draw_limb(painter, hip_x, 62, 13, ang, 3.5, limb_color)
            # 小脚丫
            painter.setPen(Qt.NoPen)
            painter.setBrush(limb_color)
            painter.drawEllipse(int(fx - 2.5), int(fy - 1.5), 5, 4)

        # ── 手臂（身体两侧，头会盖住肩部衔接）──
        waving = self._wave >= 0
        for side, sh_x in (("L", 28), ("R", 52)):
            ang = arm_angle(self.state, self._phase, self._dragging, waving, self._wave, side)
            hx, hy = self._draw_limb(painter, sh_x, 53, 16, ang, 3.5, limb_color)
            # 小手
            painter.setPen(Qt.NoPen)
            painter.setBrush(limb_color)
            painter.drawEllipse(int(hx - 2.5), int(hy - 2.5), 5, 5)

        # ── 身体（先画，被头压住上半）──
        body = QColor("#23262f")
        body.setAlpha(235)
        painter.setBrush(body)
        painter.setPen(QPen(QColor("#3a3f4b"), 1))
        painter.drawRoundedRect(28, 50, 24, 14, 7, 7)

        # 指示灯（状态色）
        painter.setBrush(QColor(STATE_COLORS.get(self.state, STATE_COLORS["idle"])))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(37, 55, 6, 6)

        # ── 天线 ──
        painter.setPen(QPen(QColor("#4b5563"), 2))
        painter.drawLine(40, 14, 40, 7)
        antenna = QColor("#4d7cff")
        if self.state == "thinking":
            antenna = QColor("#fbbf24")
        painter.setBrush(antenna)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(37, 1, 6, 6)

        # ── 头部 ──
        head = QColor("#23262f")
        head.setAlpha(240)
        painter.setBrush(head)
        painter.setPen(QPen(QColor("#3a3f4b"), 1))
        painter.drawRoundedRect(18, 14, 44, 38, 12, 12)

        # ── 眼睛（LED 大眼 + 高光；眨眼 = 高度压扁）──
        eye_h = 11.0 * (1.0 - 0.85 * abs(math.sin(self._blink * math.pi)))
        eye_w = 11.0
        eye_color = QColor("#4d7cff") if self.state != "thinking" else QColor("#fbbf24")
        painter.setBrush(eye_color)
        painter.setPen(Qt.NoPen)
        for ex in (26, 43):
            painter.drawEllipse(int(ex), int(27 + (11 - eye_h) / 2), int(eye_w), max(2, int(eye_h)))
        # 高光
        if self._blink == 0:
            painter.setBrush(QColor(255, 255, 255, 160))
            for ex in (29, 46):
                painter.drawEllipse(ex, 29, 3, 3)

        # ── 嘴巴（随状态：微笑 / 思考圆 / 平线）──
        painter.setPen(QPen(QColor("#8b93a3"), 2))
        if self.state == "thinking":
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(37, 42, 6, 6)
        else:
            painter.drawArc(34, 39, 12, 9, 200 * 16, 140 * 16)

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
        """右键菜单：打开面板 / 退出。"""
        menu = QMenu(self)
        menu.addAction("打开/收起面板", self.toggle_panel)
        menu.addSeparator()
        menu.addAction("退出", QApplication.quit)
        menu.exec(event.globalPos())

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
