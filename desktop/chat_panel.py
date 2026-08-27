"""聊天气泡面板：半透明无边框窗口，接入服务器 /api/chat。

v0.6 体验修正：
- 气泡式消息（用户右/助手左，圆角气泡）+ 时间戳
- 打开时只加载最近 10 条历史，自动定位到最新消息
- 统计/周报改为独立弹窗（不混入聊天流）
- ✕ 按钮与 Esc 关闭；聊天中联动机器人状态灯
"""
import html as html_lib

import markdown as md_lib

from PySide6.QtCore import QSettings, QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QScrollArea,
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


class _GreetingWorker(QThread):
    """后台拉个性化问候。"""
    done = Signal(str)

    def __init__(self, client: ApiClient) -> None:
        super().__init__()
        self.client = client

    def run(self) -> None:
        try:
            self.done.emit(self.client.greeting())
        except Exception:
            self.done.emit("")  # 失败静默，保留默认问候


class _ExecResultWorker(QThread):
    """后台轮询执行器结果（since_id 之后的已执行指令）。"""
    done = Signal(int, list)

    def __init__(self, client: ApiClient, since_id: int) -> None:
        super().__init__()
        self.client = client
        self.since_id = since_id

    def run(self) -> None:
        try:
            self.done.emit(self.since_id, self.client.executor_results(self.since_id))
        except Exception:
            self.done.emit(self.since_id, [])


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
            elif self.mode == "daily":
                d = self.client.latest_daily()
                if not d:
                    text = "暂无今日小结。每晚 22:00 自动生成。"
                else:
                    text = f"🌙 {d['date']} 小结：<br>" + d["content"].replace("\n", "<br>")
                self.done.emit("daily", text)
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
        # Dialog 而非 Tool：面板出现在任务栏（被覆盖时可一键找回）；
        # 去掉 WindowStaysOnTopHint：默认不置顶不挡路，📌 按钮可手动钉住
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(self.W, self.H)
        self.setFocusPolicy(Qt.StrongFocus)  # 无边框窗口需要显式焦点策略才能收 Esc
        self.client = ApiClient()
        self._worker = None
        self._history_worker = None
        self._api_worker = None
        self._pinned = False
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

        # 标题行 + 图钉 + 关闭按钮
        title = QLabel("🤖 Personal AI Assistant")
        pin_btn = QPushButton("📌")
        pin_btn.setFixedSize(26, 26)
        pin_btn.setToolTip("钉住窗口（始终置顶）")
        pin_btn.setCheckable(True)
        pin_btn.setStyleSheet(
            "QPushButton { background: transparent; color: #888; border: none; font-size: 14px; }"
            "QPushButton:hover { color: #fff; background: #2a2d35; border-radius: 13px; }"
            "QPushButton:checked { color: #fbbf24; background: #2a2d35; border-radius: 13px; }"
        )
        pin_btn.clicked.connect(self._toggle_pin)
        self._pin_btn = pin_btn
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
        title_row.addWidget(pin_btn)
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
        daily_btn = QPushButton("🌙 小结")
        daily_btn.setToolTip("查看今日小结（每晚 22:00 生成）")
        daily_btn.clicked.connect(self._show_daily)
        history_btn = QPushButton("🕘 历史")
        history_btn.setToolTip("展开最近 10 条对话记录")
        history_btn.clicked.connect(self._load_history)
        for b in (stats_btn, report_btn, daily_btn, history_btn):
            b.setStyleSheet(
                "QPushButton { background: #23262f; color: #aaa; border: 1px solid #333;"
                "border-radius: 8px; padding: 4px 10px; font-size: 12px; }"
                "QPushButton:hover { color: #eee; border-color: #555; }"
            )
        quick_row.addWidget(stats_btn)
        quick_row.addWidget(report_btn)
        quick_row.addWidget(daily_btn)
        quick_row.addWidget(history_btn)
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

        # 问候标签（个性化+时效，每次打开面板刷新；独立于消息流）
        self._greeting_label = QLabel("你好，我是你的个人助手")
        self._greeting_label.setWordWrap(True)
        self._greeting_label.setStyleSheet(
            "QLabel { color: #9aa3b2; font-size: 12px; padding: 6px 2px;"
            "border-bottom: 1px solid #23262f; }"
        )
        layout.addWidget(self._greeting_label)
        self._greeting_worker = None
        # 执行器结果轮询：每 10s 检查新结果，主动显示到聊天流。
        # last_executor_id 持久化（QSettings）：面板重启后不重复播报历史执行结果
        self._last_executor_id = int(
            QSettings("PersonalAI", "Assistant").value("executor_last_id", 0) or 0
        )
        self._exec_worker = None
        self._exec_timer = QTimer(self)
        self._exec_timer.timeout.connect(self._poll_executor_results)
        self._exec_timer.start(10_000)

    # ── 问候刷新（每次打开）───────────────────────────────

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.input.setFocus()
        self._refresh_greeting()

    def _poll_executor_results(self) -> None:
        """轮询执行器新结果（有 worker 防重入）。"""
        if self._exec_worker is not None:
            return
        self._exec_worker = _ExecResultWorker(self.client, self._last_executor_id)
        self._exec_worker.done.connect(self._on_exec_results)
        self._exec_worker.start()

    def _on_exec_results(self, since_id: int, results: list) -> None:
        worker = self._exec_worker
        self._exec_worker = None
        if worker:
            worker.deleteLater()  # 防 QThread 慢性泄漏
        for r in results:
            self._last_executor_id = max(self._last_executor_id, r["id"])
            mark = "✅" if r["status"] == "done" else "❌"
            self._append(
                "assistant",
                f"{mark} 执行完成：{r['action']} {r['target']}\n{r['result']}",
            )
            if self.ball:
                self.ball._blink = 0.01  # 机器人眨眼提示有结果
        QSettings("PersonalAI", "Assistant").setValue(
            "executor_last_id", self._last_executor_id
        )

    def _refresh_greeting(self) -> None:
        if self._greeting_worker is not None:
            return
        self._greeting_worker = _GreetingWorker(self.client)
        self._greeting_worker.done.connect(self._on_greeting)
        self._greeting_worker.start()

    def _on_greeting(self, text: str) -> None:
        worker = self._greeting_worker
        self._greeting_worker = None
        if worker:
            worker.deleteLater()  # 防 QThread 慢性泄漏
        if text:
            self._greeting_label.setText(f"👋 {text}")
            self._greeting_label.setToolTip("双击机器人随时重新打招呼（每次打开自动刷新）")

    def _load_history(self) -> None:
        """点「🕘 历史」按钮才加载最近 10 条。"""
        if self._history_loaded or self._history_worker is not None:
            return
        self._history_loaded = True
        self._append("assistant", "正在加载历史…")
        self._history_worker = _HistoryWorker(self.client)
        self._history_worker.done.connect(self._on_history)
        self._history_worker.start()

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
        """气泡式消息：用户右蓝、助手左灰，带时间戳。raw=True 时 text 为可信 HTML。

        助手消息按 Markdown 渲染（加粗/表格/列表/链接），解决"输出凌乱"。
        统一 strip 首尾空白：前导空格会被 Markdown 当成缩进代码块渲染，走样。
        """
        text = (text or "").strip()
        if raw:
            rendered = text
        elif role == "assistant":
            # Markdown → HTML（表格/加粗/代码块/列表均可渲染）
            rendered = md_lib.markdown(text, extensions=["tables", "fenced_code"])
        else:
            rendered = html_lib.escape(text).replace("\n", "<br>")
        if role == "user":
            bubble = (
                f'<div style="text-align:right;margin:6px 0;">'
                f'<span style="display:inline-block;background:#2b5cff;color:#fff;'
                f'border-radius:12px;padding:7px 12px;max-width:82%;'
                f'text-align:left;border-bottom-right-radius:4px;">{rendered}</span><br>'
                f'<span style="font-size:10px;color:#5b6270;">{_fmt_ts(ts)}</span></div>'
            )
        else:
            bubble = (
                f'<div style="text-align:left;margin:6px 0;">'
                f'<span style="display:inline-block;background:#23262f;color:#d8dbe2;'
                f'border-radius:12px;padding:7px 12px;max-width:82%;'
                f'border-bottom-left-radius:4px;">{rendered}</span><br>'
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

    def _show_daily(self) -> None:
        self._run_api("daily")

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
        title = {"stats": "📊 今日概览", "report": "📋 周报", "daily": "🌙 今日小结"}.get(mode, "信息")
        dlg = _InfoDialog(title, text, parent=self)
        dlg.show()

    # ── 发送与状态联动 ─────────────────────────────────────

    def _send(self) -> None:
        msg = self.input.text().strip()
        if not msg or self._worker is not None:
            return
        self.input.clear()
        self._append("user", msg, "")

        # 本地执行器优先：执行类命令在本地直行，不经过服务器
        # （零延迟 + 文件内容不出本机 + 服务器挂机也可用）
        from local_exec import try_execute

        handled, text = try_execute(msg)
        if handled:
            self._append("assistant", text, "")
            if self.ball:
                self.ball.set_state("online")
            return

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

    def _toggle_pin(self, checked: bool) -> None:
        """图钉开关：置顶/取消置顶（setWindowFlag 后需 show 刷新）。"""
        self._pinned = checked
        self.setWindowFlag(Qt.WindowStaysOnTopHint, checked)
        self.show()
        self.raise_()
        self._pin_btn.setToolTip("已钉住（始终置顶）" if checked else "钉住窗口（始终置顶）")
