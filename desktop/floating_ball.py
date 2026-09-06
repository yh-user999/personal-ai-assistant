"""悬浮机器人：无边框透明窗口，可拖拽，点击展开聊天面板。

形象：QPainter 自绘机器人，双皮肤（右键菜单切换，QSettings 持久化）——
- bender：班德金属风（金属灰桶形头 + 视窗单眼 + 格栅嘴）
- astro：白色宇航员风（原创致敬：白盔 + 琥珀面罩 + 黑圆眼 + 黄色点缀）
动作：思考=右手托下巴、被拖拽=双臂上举+荡腿、双击=招手、呼吸摆动。
姿态计算在 robot_pose.py（纯数学，可单测），本文件只负责画。
"""
import math
import random
import webbrowser
from pathlib import Path

import ball_painters
import theme
from chat_panel import ChatPanel
from chat_workers import _HealthWorker, _NovelWorkbenchWorker, retire
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QWidget
from skins import SKIN_NAMES, current_skin, set_skin
from theme import THEME_NAMES

# 状态 → 主色（光晕/天线/眼睛/胸口屏联动）——跟随主题（theme.state_colors）
_DEFAULT_STATE_COLORS = {
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
        self._novel_worker = None
        self._health_timer = QTimer(self)
        self._health_timer.timeout.connect(self._check_health)
        self._health_timer.start(60_000)
        self._check_health()
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._shutdown)

        # 默认位置：屏幕右下角
        screen = self.screen() or QApplication.primaryScreen()
        geo = screen.availableGeometry()
        self.move(geo.right() - self.W - 40, geo.bottom() - self.H - 40)

    # ── 对外接口 ───────────────────────────────────────────

    def set_state(self, state: str) -> None:
        """切换状态指示灯颜色：idle/online/thinking/error。"""
        if state in theme.state_colors():
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

    # ── 小说工作台 ─────────────────────────────────────────

    def open_novel_workbench(self) -> None:
        """后台建立/复用隧道，成功后用系统默认浏览器打开工作台。"""
        if self._novel_worker is not None:
            return
        self._novel_worker = _NovelWorkbenchWorker(self._health_client)
        self._novel_worker.done.connect(self._on_novel_workbench)
        self._novel_worker.start()

    def _on_novel_workbench(self, url: str, error: str) -> None:
        worker = self._novel_worker
        self._novel_worker = None
        if worker:
            retire(worker)
        if error:
            QMessageBox.warning(self, "小说工作台", error)
            return
        try:
            opened = webbrowser.open(url, new=2)
        except Exception:
            opened = False
        if not opened:
            QMessageBox.warning(self, "小说工作台", "默认浏览器未能打开工作台页面")

    def _shutdown(self) -> None:
        """退出时只清理本进程创建的 worker/SSH 隧道。"""
        for worker in (self._novel_worker, self._health_worker):
            if worker is not None:
                worker.wait(12_000)
        self._health_client.close_novel_tunnel()

    # ── 动画 ───────────────────────────────────────────────

    def _tick(self) -> None:
        # 体贴模式（caring）呼吸放缓：状态感从节奏上也能读出来
        self._phase += 0.04 if self.state == "caring" else 0.06
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

    def _state_color(self) -> QColor:
        colors = theme.state_colors()
        return QColor(colors.get(self.state, colors["idle"]))

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

        painter_fn = ball_painters.PAINTERS.get(self.skin, ball_painters.paint_bender)
        painter_fn(self, painter, accent)

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
        menu.addAction("小说工作台", self.open_novel_workbench)
        skin_menu = menu.addMenu("换肤")
        for name, label in SKIN_NAMES.items():
            act = skin_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(self.skin == name)
            act.triggered.connect(lambda checked=False, n=name: self._switch_skin(n))
        theme_menu = menu.addMenu("主题")
        for name, label in THEME_NAMES.items():
            act = theme_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(theme.current_theme() == name)
            act.triggered.connect(lambda checked=False, n=name: self._switch_theme(n))
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

    def _switch_theme(self, name: str) -> None:
        """切换主题：持久化 + 状态灯/LED 重绘 + 打开中的面板即时重着色 + 托盘图标。"""
        if name not in THEME_NAMES or name == theme.current_theme():
            return
        theme.set_theme(name)
        self.update()  # 光晕/LED/粒子取 theme.state_colors()，重绘即生效
        if self.panel is not None:
            self.panel.apply_theme()
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
