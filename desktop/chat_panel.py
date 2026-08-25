"""聊天气泡面板：半透明无边框窗口，接入服务器 /api/chat。

v0.6 体验修正：
- 气泡式消息（用户右/助手左，圆角气泡）+ 时间戳
- 打开时只加载最近 10 条历史，自动定位到最新消息
- 统计/周报改为独立弹窗（不混入聊天流）
- ✕ 按钮与 Esc 关闭；聊天中联动机器人状态灯
"""
import html as html_lib

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QTextBrowser, QVBoxLayout, QWidget,
)

from api_client import ApiClient


class _InfoDialog(QDialog):
    """统计/周报独立弹窗：不污染聊天流，可滚动可关闭。"""

    def __init__(self, title: str, html_text: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        self.resize(440, 520)
        lay = QVBoxLayout(self)
        browser = QTextBrowser()
        browser.setHtml(html_text)
        browser.setStyleSheet("background: #14161b; color: #d8dbe2; border: none; font-size: 13px;")
        lay.addWidget(browser, 1)
        close_btn = QPushButton("关闭（Esc）")
        close_btn.clicked.connect(self.close)
        close_btn.setStyleSheet(
            "QPushButton { background: #23262f; color: #ccc; border: 1px solid #333;"
            "border-radius: 8px; padding: 8px; }"
            "QPushButton:hover { color: #fff; }"
        )
        lay.addWidget(close_btn)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)


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


class _HistoryWorker(QThread):
    """后台加载历史消息。"""
    done = Signal(list)

    def __init__(self, client: ApiClient) -> None:
        super().__init__()
        self.client = client

    def run(self) -> None:
        try:
            self.done.emit(self.client.recent_messages(10))  # 只加载最近 10 条
        except Exception:
            self.done.emit([])


class _ApiWorker(QThread):
    """后台执行快捷查询（统计/周报），避免 UI 线程网络阻塞。"""
    done = Signal(str, str)  # (mode, text)

    def __init__(self, client: ApiClient, mode: str) -> None:
        super().__init__()
        self.client = client
        self.mode = mode

    def run(self) -> None:
        try:
            if self.mode == "stats":
                d = self.client.stats_summary(7)
                text = (
                    f"📊 近 7 天统计：<br>对话 {d['messages']} 条 · git 提交 {d['git_commits']} 次 · "
                    f"工作日志 {d['work_logs']} 条<br><br><b>应用时长 Top：</b><br>"
                    + "".join(f"· {a['name']}: {a['hours']}h<br>" for a in d.get("top_apps", []))
                    + "<br><b>浏览域名 Top：</b><br>"
                    + "".join(f"· {b['name']}: {b['count']} 次<br>" for b in d.get("top_domains", []))
                )
                self.done.emit("stats", text)
            else:  # report
                r = self.client.latest_report()
                if not r:
                    text = "暂无周报。每周日 21:00 自动生成，敬请期待。"
                else:
                    text = f"📋 周报 {r.get('week', '')}：<br>{r.get('content', '')}"
                self.done.emit("report", text)
        except Exception as e:
            self.done.emit(self.mode, f"[获取失败] {e}")


def _fmt_ts(ts: str) -> str:
    """ISO 时间 → HH:MM（取 UTC 小时直接显示，个人使用足够）。"""
    try:
        return ts[11:16]
    except Exception:
        return ""


class ChatPanel(QWidget):
    W, H = 460, 600

    def __init__(self, ball=None) -> None:
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(self.W, self.H)
        self.setFocusPolicy(Qt.StrongFocus)  # 无边框窗口需要显式焦点策略才能收 Esc
        self.client = ApiClient()
        self._worker = None
        self._history_worker = None
        self._api_worker = None
        self.ball = ball  # 悬浮机器人引用：聊天时联动状态灯/表情
        self._history_loaded = False
        self._init_ui()

    # ── UI 构建 ────────────────────────────────────────────

    def _init_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QWidget()
        card.setObjectName("card")
        card.setStyleSheet(
            "#card { background: rgba(28, 31, 38, 245); border-radius: 14px; }"
            "QLabel { color: #eee; }"
        )
        outer.addWidget(card)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 10, 14, 10)

        # 标题行 + 关闭按钮
        title = QLabel("🤖 Personal AI Assistant")
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(26, 26)
        close_btn.setToolTip("关闭（Esc）")
        close_btn.setStyleSheet(
            "QPushButton { background: transparent; color: #888; border: none; font-size: 15px; }"
            "QPushButton:hover { color: #fff; background: #2a2d35; border-radius: 13px; }"
        )
        close_btn.clicked.connect(self.hide)
        title_row = QHBoxLayout()
        title_row.addWidget(title, 1)
        title_row.addWidget(close_btn)
        layout.addLayout(title_row)

        # 消息区
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        # 防内存无限增长：最多保留 500 段，超出自动丢弃最早内容
        self.browser.document().setMaximumBlockCount(500)
        self.browser.setStyleSheet(
            "background: transparent; border: none; color: #eee; font-size: 13px;"
        )
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.browser)
        scroll.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(scroll, 1)

        # 快捷按钮行
        quick_row = QHBoxLayout()
        stats_btn = QPushButton("📊 今日")
        stats_btn.setToolTip("查看近 7 天行为统计")
        stats_btn.clicked.connect(self._show_stats)
        report_btn = QPushButton("📋 周报")
        report_btn.setToolTip("查看最新周报")
        report_btn.clicked.connect(self._show_report)
        for b in (stats_btn, report_btn):
            b.setStyleSheet(
                "QPushButton { background: #23262f; color: #aaa; border: 1px solid #333;"
                "border-radius: 8px; padding: 4px 10px; font-size: 12px; }"
                "QPushButton:hover { color: #eee; border-color: #555; }"
            )
        quick_row.addWidget(stats_btn)
        quick_row.addWidget(report_btn)
        quick_row.addStretch(1)
        layout.addLayout(quick_row)

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
        send_btn.setStyleSheet(
            "background: #2b5cff; color: white; border: none; border-radius: 8px; padding: 0 16px;"
        )
        send_btn.clicked.connect(self._send)
        row.addWidget(self.input, 1)
        row.addWidget(send_btn)
        layout.addLayout(row)

    # ── 历史加载 ───────────────────────────────────────────

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._history_loaded:
            self._history_loaded = True
            self._append("assistant", "正在加载历史…")
            self._history_worker = _HistoryWorker(self.client)
            self._history_worker.done.connect(self._on_history)
            self._history_worker.start()
        # 每次打开定位到最新消息（不留在旧滚动位置）
        sb = self.browser.verticalScrollBar()
        sb.setValue(sb.maximum())
        self.input.setFocus()

    def _on_history(self, messages: list) -> None:
        worker = self._history_worker
        self._history_worker = None
        if worker:
            worker.deleteLater()  # 防 QThread 慢性泄漏
        self.browser.clear()
        if not messages:
            self._append(
                "assistant",
                "你好，我是你的个人助手。我可以记住我们的对话、记录你的工作日志，"
                "每周生成学习反思。试试说\"记录：下午2-5点调RAG性能\"。",
            )
            return
        for m in messages:
            self._append(m.get("sender", "assistant"), m.get("content", ""), m.get("ts", ""))

    # ── 消息与气泡 ─────────────────────────────────────────

    def _append(self, role: str, text: str, ts: str = "", raw: bool = False) -> None:
        """气泡式消息：用户右蓝、助手左灰，带时间戳。raw=True 时 text 为可信 HTML。"""
        text_esc = text if raw else html_lib.escape(text).replace("\n", "<br>")
        if role == "user":
            bubble = (
                f'<div style="text-align:right;margin:6px 0;">'
                f'<span style="display:inline-block;background:#2b5cff;color:#fff;'
                f'border-radius:12px;padding:7px 12px;max-width:82%;'
                f'text-align:left;border-bottom-right-radius:4px;">{text_esc}</span><br>'
                f'<span style="font-size:10px;color:#5b6270;">{_fmt_ts(ts)}</span></div>'
            )
        else:
            bubble = (
                f'<div style="text-align:left;margin:6px 0;">'
                f'<span style="display:inline-block;background:#23262f;color:#d8dbe2;'
                f'border-radius:12px;padding:7px 12px;max-width:82%;'
                f'border-bottom-left-radius:4px;">{text_esc}</span><br>'
                f'<span style="font-size:10px;color:#5b6270;">{_fmt_ts(ts)}</span></div>'
            )
        self.browser.append(bubble)
        sb = self.browser.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ── 快捷功能 ───────────────────────────────────────────

    def _show_stats(self) -> None:
        self._run_api("stats")

    def _show_report(self) -> None:
        self._run_api("report")

    def _run_api(self, mode: str) -> None:
        """快捷查询统一走后台线程，结果弹独立窗口（不混入聊天流）。"""
        worker = _ApiWorker(self.client, mode)
        worker.done.connect(self._on_api_done)
        self._api_worker = worker
        worker.start()

    def _on_api_done(self, mode: str, text: str) -> None:
        worker = self._api_worker
        self._api_worker = None
        if worker:
            worker.deleteLater()
        title = "📊 今日概览" if mode == "stats" else "📋 周报"
        dlg = _InfoDialog(title, text, parent=self)
        dlg.show()

    # ── 发送与状态联动 ─────────────────────────────────────

    def _send(self) -> None:
        msg = self.input.text().strip()
        if not msg or self._worker is not None:
            return
        self.input.clear()
        self._append("user", msg, "")
        if self.ball:
            self.ball.set_state("thinking")  # 机器人进入思考状态（琥珀灯+圆嘴）
        self._worker = _ChatWorker(self.client, msg)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, role: str, text: str) -> None:
        worker = self._worker
        self._worker = None
        if worker:
            worker.deleteLater()  # 防 QThread 慢性泄漏
        self._append(role, text, "")
        if self.ball:
            self.ball.set_state("online")  # 回复完成 → 绿灯

    # ── 键盘 ───────────────────────────────────────────────

    def keyPressEvent(self, event) -> None:
        """Esc 关闭面板。"""
        if event.key() == Qt.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)
