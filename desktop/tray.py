"""系统托盘：自绘机器人图标 + 菜单 + 新周报通知。

v0.5：真正接入桌面端——托盘常驻、打开面板、今日概览（面板内）、新周报弹通知。
v0.6：周报检查线程化（不在 UI 线程做网络请求）+ QThread deleteLater。
v0.7：bender/astro 图标与悬浮球新形象同步（平顶机械头 / 圆球头盔玻璃面罩）。
"""
import math

from PySide6.QtCore import QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon


class _ReportWorker(QThread):
    """后台检查最新周报 + 每日小结。"""
    done = Signal(object, object)  # (report, daily)

    def __init__(self, client) -> None:
        super().__init__()
        self.client = client

    def run(self) -> None:
        try:
            self.done.emit(self.client.latest_report(), self.client.latest_daily())
        except Exception:
            self.done.emit(None, None)


def make_robot_icon(size: int = 64, skin: str = "bender") -> QIcon:
    """自绘机器人托盘图标（三皮肤，与悬浮球/迷你头像同形象）。"""
    from PySide6.QtGui import QLinearGradient, QPainterPath
    from skins import current_skin

    if skin == "auto":
        skin = current_skin()
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    stroke = QColor("#565e70")
    if skin == "astro":
        # 白色宇航员风 · 重制版：圆球头盔 + 深色玻璃面罩 + 发光圆眼
        yellow = QColor("#f5c518")
        # 天线
        p.setPen(QColor("#9aa5b8"))
        p.drawLine(int(size * 0.5), int(size * 0.13), int(size * 0.5), int(size * 0.05))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#4d7cff"))
        p.drawEllipse(QRectF(size * 0.43, 0.0, size * 0.14, size * 0.12))
        # 头盔圆球
        g = QLinearGradient(0, 0, size, size)
        g.setColorAt(0.0, QColor("#ffffff"))
        g.setColorAt(0.6, QColor("#e6ebf3"))
        g.setColorAt(1.0, QColor("#c2cad8"))
        p.setBrush(g)
        p.setPen(QColor("#9aa5b8"))
        p.drawEllipse(QRectF(size * 0.13, size * 0.14, size * 0.74, size * 0.74))
        # 侧耳灯
        p.setBrush(yellow)
        p.setPen(QColor("#c99e14"))
        p.drawEllipse(QRectF(size * 0.045, size * 0.44, size * 0.13, size * 0.13))
        p.drawEllipse(QRectF(size * 0.825, size * 0.44, size * 0.13, size * 0.13))
        # 深色玻璃面罩
        vg = QLinearGradient(0, size * 0.30, 0, size * 0.66)
        vg.setColorAt(0.0, QColor("#2b3550"))
        vg.setColorAt(1.0, QColor("#151b29"))
        p.setBrush(vg)
        p.setPen(QColor("#10141f"))
        p.drawRoundedRect(QRectF(size * 0.31, size * 0.32, size * 0.38, size * 0.36),
                          size * 0.09, size * 0.09)
        # 眼睛（发光圆眼）+ 微笑
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#e8f4ff"))
        p.drawEllipse(QRectF(size * 0.385, size * 0.40, size * 0.08, size * 0.13))
        p.drawEllipse(QRectF(size * 0.535, size * 0.40, size * 0.08, size * 0.13))
        p.setBrush(QColor(255, 255, 255, 230))
        p.drawEllipse(QRectF(size * 0.40, size * 0.42, size * 0.03, size * 0.03))
        p.drawEllipse(QRectF(size * 0.55, size * 0.42, size * 0.03, size * 0.03))
        p.setPen(QColor("#dfe9ff"))
        p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(size * 0.44, size * 0.55, size * 0.12, size * 0.08),
                  200 * 16, 140 * 16)
    elif skin == "classic":
        # 原版萌系风：暗色圆角头 + 双 LED 眼 + 微笑
        g = QLinearGradient(0, 0, size, size)
        g.setColorAt(0.0, QColor("#4a5266"))
        g.setColorAt(1.0, QColor("#1a1d24"))
        stroke = QColor("#4a5264")
        p.setBrush(g)
        p.setPen(stroke)
        p.drawRoundedRect(8, 16, size - 16, int(size * 0.55), 10, 10)
        p.setPen(QColor("#4b5563"))
        p.drawLine(size // 2, 8, size // 2, 16)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#4d7cff"))
        p.drawEllipse(size // 2 - 4, 4, 8, 8)
        p.setBrush(QColor("#4d7cff"))
        p.drawEllipse(int(size * 0.32), int(size * 0.42), int(size * 0.16), int(size * 0.16))
        p.drawEllipse(int(size * 0.52), int(size * 0.42), int(size * 0.16), int(size * 0.16))
        p.setPen(QColor("#8b93a3"))
        p.drawArc(int(size * 0.36), int(size * 0.52), int(size * 0.28), int(size * 0.16),
                  200 * 16, 140 * 16)
    else:
        # 班德金属风 · 硬核机械版：平顶切角头 + 内嵌小屏 + 散热格栅
        g = QLinearGradient(0, 0, size, size)
        g.setColorAt(0.0, QColor("#b7c0d4"))
        g.setColorAt(0.5, QColor("#646e82"))
        g.setColorAt(1.0, QColor("#2f3542"))
        stroke = QColor("#525a6b")
        # 天线（方形光点）
        p.setPen(QColor("#525a6b"))
        p.drawLine(int(size * 0.5), int(size * 0.16), int(size * 0.5), int(size * 0.06))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#4d7cff"))
        p.drawRect(QRectF(size * 0.455, 0.0, size * 0.09, size * 0.07))
        # 侧六角螺栓
        for bx in (0.16, 0.84):
            hex_path = QPainterPath()
            for i in range(6):
                ang = math.radians(60 * i)
                px = size * bx + size * 0.055 * math.cos(ang)
                py = size * 0.43 + size * 0.055 * math.sin(ang)
                if i == 0:
                    hex_path.moveTo(px, py)
                else:
                    hex_path.lineTo(px, py)
            hex_path.closeSubpath()
            p.setBrush(g)
            p.setPen(QColor("#525a6b"))
            p.drawPath(hex_path)
        # 平顶切角头
        head = QPainterPath()
        head.moveTo(size * 0.29, size * 0.16)
        head.lineTo(size * 0.71, size * 0.16)
        head.lineTo(size * 0.77, size * 0.22)
        head.lineTo(size * 0.77, size * 0.64)
        head.lineTo(size * 0.71, size * 0.70)
        head.lineTo(size * 0.29, size * 0.70)
        head.lineTo(size * 0.23, size * 0.64)
        head.lineTo(size * 0.23, size * 0.22)
        head.closeSubpath()
        p.setBrush(g)
        p.setPen(QColor("#525a6b"))
        p.drawPath(head)
        # 顶板拼缝
        p.setPen(QColor(0, 0, 0, 80))
        p.drawLine(int(size * 0.28), int(size * 0.26), int(size * 0.72), int(size * 0.26))
        # 内嵌屏 + LED 扫描眼
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#232833"))
        p.drawRoundedRect(QRectF(size * 0.32, size * 0.33, size * 0.36, size * 0.15),
                          size * 0.02, size * 0.02)
        p.setBrush(QColor("#12151c"))
        p.drawRoundedRect(QRectF(size * 0.34, size * 0.35, size * 0.32, size * 0.11),
                          size * 0.01, size * 0.01)
        p.setBrush(QColor("#4d7cff"))
        p.drawRoundedRect(QRectF(size * 0.37, size * 0.385, size * 0.26, size * 0.05),
                          size * 0.01, size * 0.01)
        # 散热格栅（竖栅）
        p.setBrush(QColor("#232833"))
        p.drawRoundedRect(QRectF(size * 0.40, size * 0.52, size * 0.20, size * 0.10),
                          size * 0.01, size * 0.01)
        p.setPen(QColor("#565e70"))
        for i in range(5):
            gx = size * (0.435 + 0.0325 * i)
            p.drawLine(int(gx), int(size * 0.545), int(gx), int(size * 0.60))
    p.end()
    return QIcon(pm)


class TrayIcon(QSystemTrayIcon):
    def __init__(self, ball, parent=None) -> None:
        super().__init__(make_robot_icon(skin="auto"), parent)
        self.ball = ball
        self.setToolTip("Personal AI Assistant")
        self._known_week = None
        self._known_daily_date = None
        self._report_worker = None

        menu = QMenu()
        menu.addAction("打开面板", self._open_panel)
        menu.addAction("显示机器人", self._show_ball)  # 图标意外消失时的恢复入口
        menu.addAction("今日概览", self._open_stats)
        menu.addSeparator()
        menu.addAction("退出", QApplication.quit)
        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)

        # 新周报通知：每 30 分钟检查一次
        self._report_timer = QTimer(self)
        self._report_timer.timeout.connect(self._check_new_report)
        self._report_timer.start(30 * 60_000)
        self._check_new_report()

    def _open_panel(self) -> None:
        self.ball.show()      # 打开面板时顺带把机器人窗口找回来（防窗口意外丢失）
        self.ball.raise_()
        self.ball.open_panel()

    def _show_ball(self) -> None:
        self.ball.show()
        self.ball.raise_()

    def refresh_icon(self) -> None:
        """换肤后刷新托盘图标。"""
        self.setIcon(make_robot_icon(skin="auto"))

    def _open_stats(self) -> None:
        self.ball.open_panel()
        if self.ball.panel:
            self.ball.panel._show_stats()

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self._open_panel()

    def _check_new_report(self) -> None:
        """发现新周报 → 托盘通知（后台线程，不卡 UI）。"""
        if self._report_worker is not None:
            return
        self._report_worker = _ReportWorker(self.ball._health_client)
        self._report_worker.done.connect(self._on_report)
        self._report_worker.start()

    def _on_report(self, report, daily) -> None:
        worker = self._report_worker
        self._report_worker = None
        if worker:
            worker.deleteLater()  # 防 QThread 慢性泄漏
        # 新周报
        if report and report.get("week") != self._known_week:
            self._known_week = report.get("week")
            self.showMessage(
                "📋 新周报已生成",
                f"《{report.get('week', '')} 学习进度反思》已就绪，点击托盘菜单查看",
                QSystemTrayIcon.Information,
                8000,
            )
        # 新每日小结（每晚 22:00 后第一次检查时通知）
        if daily and daily.get("date") != self._known_daily_date:
            self._known_daily_date = daily.get("date")
            content = (daily.get("content") or "").strip()
            preview = content[:60] + ("…" if len(content) > 60 else "")
            self.showMessage(
                "🌙 今日小结已生成",
                preview or "点击托盘菜单查看",
                QSystemTrayIcon.Information,
                8000,
            )
