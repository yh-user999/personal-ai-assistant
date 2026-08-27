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


def make_robot_icon(size: int = 64) -> QIcon:
    """自绘班德式机器人头像做托盘图标（金属灰 + 视窗眼 + 格栅嘴）。"""
    from PySide6.QtGui import QLinearGradient, QPainterPath

    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    # 头：圆顶窄顶 + 底部外扩（桶形）
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
    g = QLinearGradient(0, 0, size, size)
    g.setColorAt(0.0, QColor("#a9b2c4"))
    g.setColorAt(0.5, QColor("#6b7488"))
    g.setColorAt(1.0, QColor("#3a414e"))
    p.setBrush(g)
    p.setPen(QColor("#565e70"))
    p.drawPath(head)
    # 天线
    p.setPen(QColor("#565e70"))
    p.drawLine(size // 2, int(size * 0.14), size // 2, int(size * 0.04))
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#4d7cff"))
    p.drawEllipse(int(size * 0.44), 0, int(size * 0.12), int(size * 0.12))
    # 视窗眼（发光屏）
    p.setBrush(QColor("#20242c"))
    p.drawRoundedRect(int(size * 0.30), int(size * 0.40), int(size * 0.40), int(size * 0.18), 4, 4)
    p.setBrush(QColor("#4d7cff"))
    p.drawRoundedRect(int(size * 0.33), int(size * 0.43), int(size * 0.34), int(size * 0.12), 3, 3)
    # 格栅嘴（2 条横槽）
    p.setPen(QColor("#565e70"))
    p.drawLine(int(size * 0.32), int(size * 0.64), int(size * 0.68), int(size * 0.64))
    p.drawLine(int(size * 0.32), int(size * 0.70), int(size * 0.68), int(size * 0.70))
    p.end()
    return QIcon(pm)


class TrayIcon(QSystemTrayIcon):
    def __init__(self, ball, parent=None) -> None:
        super().__init__(make_robot_icon(), parent)
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
