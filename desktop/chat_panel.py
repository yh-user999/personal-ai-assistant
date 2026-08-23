"""聊天气泡面板：半透明窗口，接入服务器 /api/chat。"""
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QTextBrowser, QVBoxLayout, QWidget,
)

from api_client import ApiClient


class _ChatWorker(QThread):
    """后台线程调服务器，避免阻塞 UI。"""
    done = Signal(str, str)  # (role, text)

    def __init__(self, client: ApiClient, message: str) -> None:
        super().__init__()
        self.client = client
        self.message = message

    def run(self) -> None:
        try:
            reply = self.client.chat(self.message)
            self.done.emit("assistant", reply)
        except Exception as e:
            self.done.emit("assistant", f"[连接失败] {e}")


class ChatPanel(QWidget):
    W, H = 440, 560

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(self.W, self.H)
        self.client = ApiClient()
        self._worker = None
        self._init_ui()

    def _init_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QWidget()
        card.setObjectName("card")
        card.setStyleSheet(
            "#card { background: rgba(28, 31, 38, 235); border-radius: 14px; }"
            "QLabel { color: #eee; }"
        )
        outer.addWidget(card)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 10, 14, 10)

        title = QLabel("🤖 Personal AI Assistant")
        layout.addWidget(title)

        # 消息区
        self.browser = QTextBrowser()
        self.browser.setStyleSheet(
            "background: transparent; border: none; color: #eee; font-size: 13px;"
        )
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.browser)
        scroll.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(scroll, 1)

        # 输入区
        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("说点什么…（记录：xxx 可记工作日志）")
        self.input.setStyleSheet(
            "background: #14161b; color: #eee; border: 1px solid #333;"
            "border-radius: 8px; padding: 8px;"
        )
        self.input.returnPressed.connect(self._send)
        send_btn = QPushButton("发送")
        send_btn.setStyleSheet("background: #2b5cff; color: white; border: none; border-radius: 8px; padding: 0 16px;")
        send_btn.clicked.connect(self._send)
        row.addWidget(self.input, 1)
        row.addWidget(send_btn)
        layout.addLayout(row)

    def _send(self) -> None:
        msg = self.input.text().strip()
        if not msg or self._worker is not None:
            return
        self.input.clear()
        self._append("user", msg)
        self._append("assistant", "…")
        self._worker = _ChatWorker(self.client, msg)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, role: str, text: str) -> None:
        self._worker = None
        self._append(role, text, replace_last=True)

    def _append(self, role: str, text: str, replace_last: bool = False) -> None:
        html = f"<p><b>{'你' if role == 'user' else '助手'}:</b><br>{text}</p>"
        if replace_last:
            # 替换末尾占位（简化：追加）
            self.browser.append(html)
        else:
            self.browser.append(html)
        self.browser.verticalScrollBar().setValue(self.browser.verticalScrollBar().maximum())
