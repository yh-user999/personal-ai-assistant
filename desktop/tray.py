"""系统托盘：自绘机器人图标 + 菜单 + 新周报通知。

v0.5：真正接入桌面端——托盘常驻、打开面板、今日概览（面板内）、新周报弹通知。
v0.6：周报检查线程化（不在 UI 线程做网络请求）+ QThread deleteLater。
"""
from PySide6.QtCore import Qt, QThread, QTimer, Signal
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
    """自绘机器人托盘图标（双皮肤：bender 金属风 / astro 白色宇航员风）。"""
    from PySide6.QtGui import QLinearGradient, QPainterPath

    from skins import current_skin

    if skin == "auto":
        skin = current_skin()
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    g = QLinearGradient(0, 0, size, size)
    stroke = QColor("#565e70")
    if skin == "astro":
        # 白色宇航员风：白盔 + 琥珀面罩 + 黑圆眼 + 黄色点缀
        g.setColorAt(0.0, QColor("#ffffff"))
        g.setColorAt(0.55, QColor("#dfe4ec"))
        g.setColorAt(1.0, QColor("#b9c1ce"))
        stroke = QColor("#9aa3b5")
        p.setBrush(g)
        p.setPen(stroke)
        p.drawRoundedRect(int(size * 0.14), int(size * 0.10), int(size * 0.72), int(size * 0.66), int(size * 0.3), int(size * 0.3))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#dfe4ec"))
        p.drawEllipse(int(size * 0.02), int(size * 0.28), int(size * 0.16), int(size * 0.16))
        p.drawEllipse(int(size * 0.82), int(size * 0.28), int(size * 0.16), int(size * 0.16))
        # 天线
        p.setPen(QColor("#9aa3b5"))
        p.drawLine(size // 2, int(size * 0.10), size // 2, int(size * 0.02))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#4d7cff"))
        p.drawEllipse(int(size * 0.44), 0, int(size * 0.12), int(size * 0.12))
        # 面罩
        p.setBrush(QColor("#f2b33d"))
        p.setPen(QColor("#b8822a"))
        p.drawRoundedRect(int(size * 0.26), int(size * 0.24), int(size * 0.48), int(size * 0.38), int(size * 0.1), int(size * 0.1))
        # 黑圆眼
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#1a1d24"))
        p.drawEllipse(int(size * 0.33), int(size * 0.33), int(size * 0.12), int(size * 0.12))
        p.drawEllipse(int(size * 0.55), int(size * 0.33), int(size * 0.12), int(size * 0.12))
        p.setBrush(QColor(255, 255, 255, 210))
        p.drawEllipse(int(size * 0.37), int(size * 0.36), int(size * 0.05), int(size * 0.05))
        p.drawEllipse(int(size * 0.59), int(size * 0.36), int(size * 0.05), int(size * 0.05))
        # 嘴
        p.setPen(QColor("#8a5a14"))
        p.drawLine(int(size * 0.43), int(size * 0.54), int(size * 0.57), int(size * 0.54))
    elif skin == "classic":
        # 原版萌系风：暗色圆角头 + 双 LED 眼 + 微笑
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
        p.drawArc(int(size * 0.36), int(size * 0.52), int(size * 0.28), int(size * 0.16), 200 * 16, 140 * 16)
    else:
        # 班德金属风：桶形头 + 视窗眼 + 格栅嘴
        g.setColorAt(0.0, QColor("#a9b2c4"))
        g.setColorAt(0.5, QColor("#6b7488"))
        g.setColorAt(1.0, QColor("#3a414e"))
        head = QPainterPath()
        head.moveTo(size * 0.22, size * 0.40)
        head.quadTo(size * 0.22, size * 0.14, size * 0.50, size * 0.14)
        head.quadTo(size * 0.78, size * 0.14, size * 0.78, size * 0.40)
        head.lineTo(size * 0.86, size * 0.72)
        head.quadTo(size * 0.86, size * 0.78, size * 0.76, size * 0.78)
        head.lineTo(size * 0.24, size * 0.78)
        head.quadTo(size * 0.14, size * 0.78, size * 0.14, size * 0.72)
        head.lineTo(size * 0.22, size * 0.40)
        head.closeSubpath()
        p.setBrush(g)
        p.setPen(stroke)
        p.drawPath(head)
        p.setPen(stroke)
        p.drawLine(size // 2, int(size * 0.14), size // 2, int(size * 0.04))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#4d7cff"))
        p.drawEllipse(int(size * 0.44), 0, int(size * 0.12), int(size * 0.12))
        p.setBrush(QColor("#20242c"))
        p.drawRoundedRect(int(size * 0.30), int(size * 0.40), int(size * 0.40), int(size * 0.18), 4, 4)
        p.setBrush(QColor("#4d7cff"))
        p.drawRoundedRect(int(size * 0.33), int(size * 0.43), int(size * 0.34), int(size * 0.12), 3, 3)
        p.setPen(stroke)
        p.drawLine(int(size * 0.32), int(size * 0.64), int(size * 0.68), int(size * 0.64))
        p.drawLine(int(size * 0.32), int(size * 0.70), int(size * 0.68), int(size * 0.70))
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
