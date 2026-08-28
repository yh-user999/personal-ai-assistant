"""聊天气泡面板：半透明无边框窗口，接入服务器 /api/chat。

v0.6 体验修正：
- 气泡式消息（用户右/助手左，圆角气泡）+ 时间戳
- 打开时只加载最近 10 条历史，自动定位到最新消息
- 统计/周报改为独立弹窗（不混入聊天流）
- ✕ 按钮与 Esc 关闭；聊天中联动机器人状态灯
"""
import html as html_lib
import re

import markdown as md_lib
from api_client import ApiClient
from chat_workers import (
    _ApiWorker,
    _ChatWorker,
    _ExecResultWorker,
    _GreetingWorker,
    _HistoryWorker,
)
from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from robot_avatar import RobotAvatar


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
            # 完全不透明（alpha=255）：背后窗口内容不会再透进来造成"叠字"，
            # 这是"面板排版看着乱"的隐藏元凶（之前 245≈96% 不透明）
            "#card { background: #1c1f26; border-radius: 14px; }"
            "QLabel { color: #eee; }"
        )
        outer.addWidget(card)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 10, 14, 10)

        # 标题行 + 图钉 + 关闭按钮（版本号用于确认面板跑的是不是最新代码）
        title = QLabel("🤖 Personal AI Assistant v4.5")
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

        # 消息区：滚动容器 + 垂直布局（每条消息 = 一行部件，左右由布局保证）
        self._avatars: list[RobotAvatar] = []   # 所有小月头像（统一动画，防内存泄漏引用）
        self._typing_row: QWidget | None = None  # "正在想"临时行（带思考动画头像）
        self._msg_container = QWidget()
        self._msg_layout = QVBoxLayout(self._msg_container)
        self._msg_layout.setContentsMargins(0, 0, 0, 0)
        self._msg_layout.setSpacing(0)
        self._msg_layout.addStretch(1)  # 底部弹簧：消息从顶部往下排
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._msg_container)
        scroll.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(scroll, 1)
        self._msg_scroll = scroll

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
        self._clear_messages()
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

    def _render(self, role: str, text: str) -> str:
        """文本 → 富文本 HTML（沿用 Markdown 渲染管线）。

        markdown 路径先中和 & 和 <：LLM 回复可能被行为数据/知识库内容注入
        原始 HTML（QTextBrowser 会按 HTML 渲染），只转义这两个字符即可阻断
        标签注入，保留 > 使 markdown 引用语法仍可用。
        """
        if role == "assistant" and re.search(r"[*#`>\[\]|]", text):
            safe = text.replace("&", "&amp;").replace("<", "&lt;")
            rendered = md_lib.markdown(safe, extensions=["tables", "fenced_code"])
            rendered = re.sub(r"(?<=[^>])\n(?=[^<])", "<br>", rendered)
            return rendered
        return html_lib.escape(text).replace("\n", "<br>")

    def _make_msg_row(self, role: str, rendered: str, ts: str = "") -> QWidget:
        """一条消息 = 一行部件：你右（蓝气泡）、小月左（灰气泡 + 动画头像）。

        左右完全由 QHBoxLayout 保证——不再依赖任何 CSS 对齐。
        """
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 3, 0, 3)
        lay.setSpacing(8)

        if role == "user":
            bg, fg = "#2b5cff", "#fff"
            align_side = Qt.AlignRight
            lay.addStretch(1)
        else:
            bg, fg = "#23262f", "#d8dbe2"
            align_side = Qt.AlignLeft
            avatar = RobotAvatar()
            self._avatars.append(avatar)
            lay.addWidget(avatar, 0, Qt.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(2)
        bubble = QLabel(rendered)
        bubble.setWordWrap(True)
        bubble.setTextFormat(Qt.RichText)
        bubble.setMaximumWidth(320)
        bubble.setContentsMargins(10, 7, 10, 7)
        bubble.setTextInteractionFlags(Qt.TextSelectableByMouse)  # 支持选中复制
        bubble.setStyleSheet(
            f"QLabel {{ background: {bg}; color: {fg}; border-radius: 12px; font-size: 13px; }}"
        )
        col.addWidget(bubble, 0, align_side)
        if ts:
            ts_label = QLabel(_fmt_ts(ts))
            ts_label.setStyleSheet("QLabel { color: #5b6270; font-size: 10px; }")
            col.addWidget(ts_label, 0, align_side)
        lay.addLayout(col)
        if role != "user":
            lay.addStretch(1)
        return row

    def _append(self, role: str, text: str, ts: str = "", raw: bool = False) -> None:
        """往聊天流追加一条消息（你右蓝、小月左灰带动画头像）。"""
        text = (text or "").strip()
        if not text and not raw:
            return
        rendered = text if raw else self._render(role, text)
        row = self._make_msg_row(role, rendered, ts)
        self._msg_layout.insertWidget(self._msg_layout.count() - 1, row)
        self._trim_messages()
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _show_typing(self) -> None:
        """等待回复时显示"正在想"行：思考动画头像 + …气泡（头像动起来的展示位）。"""
        if self._typing_row is not None:
            return
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 3, 0, 3)
        lay.setSpacing(8)
        avatar = RobotAvatar()
        avatar.set_thinking(True)
        lay.addWidget(avatar, 0, Qt.AlignTop)
        bubble = QLabel("…")
        bubble.setContentsMargins(14, 7, 14, 7)
        bubble.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bubble.setStyleSheet(
            "QLabel { background: #23262f; color: #d8dbe2; border-radius: 12px; font-size: 13px; }"
        )
        lay.addWidget(bubble, 0, Qt.AlignTop)
        lay.addStretch(1)
        self._msg_layout.insertWidget(self._msg_layout.count() - 1, row)
        self._typing_row = row
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _remove_typing(self) -> None:
        if self._typing_row is not None:
            self._typing_row.deleteLater()
            self._typing_row = None

    def _clear_messages(self) -> None:
        """清空消息区（保留底部弹簧）。"""
        self._remove_typing()
        while self._msg_layout.count() > 1:
            item = self._msg_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._avatars.clear()

    def _trim_messages(self) -> None:
        """防内存无限增长：最多保留 500 条消息，超出删除最早的。"""
        while self._msg_layout.count() > 501:  # 500 条消息 + 底部弹簧
            item = self._msg_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
                # 同步清理被删消息占用的头像引用
                if self._avatars and self._avatars[0] is not w:
                    self._avatars.pop(0)

    def _scroll_to_bottom(self) -> None:
        sb = self._msg_scroll.verticalScrollBar()
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
        self._show_typing()  # 聊天流里出现"正在想"动画头像行
        self._worker = _ChatWorker(self.client, msg)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, role: str, text: str) -> None:
        worker = self._worker
        self._worker = None
        if worker:
            worker.deleteLater()  # 防 QThread 慢性泄漏
        self._remove_typing()  # 撤下"正在想"行，换上真实回复
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
