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
    """自绘一个小机器人脸做托盘图标（无外部素材）。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    # 头
    p.setBrush(QColor("#23262f"))
    p.setPen(QColor("#3a3f4b"))
    p.drawRoundedRect(8, 16, size - 16, int(size * 0.55), 10, 10)
    # 天线
    p.setPen(QColor("#4d7cff"))
    p.drawLine(size // 2, 8, size // 2, 16)
    p.setBrush(QColor("#4d7cff"))
    p.drawEllipse(size // 2 - 4, 4, 8, 8)
    # 眼睛
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#4d7cff"))
    p.drawEllipse(int(size * 0.32), int(size * 0.42), int(size * 0.16), int(size * 0.16))
    p.drawEllipse(int(size * 0.52), int(size * 0.42), int(size * 0.16), int(size * 0.16))
    # 微笑
    p.setPen(QColor("#8b93a3"))
    p.drawArc(int(size * 0.36), int(size * 0.52), int(size * 0.28), int(size * 0.16), 200 * 16, 140 * 16)
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
        self.ball.open_panel()

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
