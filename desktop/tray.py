"""系统托盘：打开面板 / 今日概览 / 周报 / 退出。"""
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


class TrayIcon(QSystemTrayIcon):
    def __init__(self, parent=None) -> None:
        super().__init__(QIcon.fromTheme("dialog-information"), parent)
        self.setToolTip("Personal AI Assistant")
        menu = QMenu()

        act_open = QAction("打开面板", menu)
        act_open.triggered.connect(self._on_open)
        menu.addAction(act_open)

        act_stats = QAction("今日概览", menu)
        act_stats.triggered.connect(self._on_stats)
        menu.addAction(act_stats)

        act_quit = QAction("退出", menu)
        act_quit.triggered.connect(self._on_quit)
        menu.addAction(act_quit)

        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)

    def _on_open(self) -> None:
        parent = self.parent()
        if parent and hasattr(parent, "open_panel"):
            parent.open_panel()

    def _on_stats(self) -> None:
        self.showMessage("今日概览", "功能开发中（M2）…", QSystemTrayIcon.Information, 3000)

    def _on_quit(self) -> None:
        QApplication_quit()

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self._on_open()


def QApplication_quit() -> None:
    from PySide6.QtWidgets import QApplication

    QApplication.quit()
