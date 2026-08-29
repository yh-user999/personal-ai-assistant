"""聊天气泡面板：半透明无边框窗口，接入服务器 /api/chat。

v0.8 主题系统：
- 4 套配色（星夜蓝/森屿绿/暮山紫/墨玉金），右键悬浮球 → 主题 切换
- theme.py 色表 + QSettings 记忆；皮肤(造型) × 主题(颜色) 正交组合
- 静态样式构建收拢 _init_static_styles，apply_theme 即时重着色不重启

v0.7 面板重制：
- 窗口可自由缩放：八方向缩放把手压在卡片不透明像素上（确定性可命中），
  纯手动几何计算缩放（不依赖系统缩放调用与分层窗口命中测试行为），
  双击标题栏最大化/还原；尺寸记忆（QSettings，下次打开恢复）
- 标题栏按住可拖动移动窗口
- 气泡宽度随窗口自适应（72% 视口宽），放大面板不再大片留白
- 深色细滚动条 / 输入框聚焦高亮 / 发送按钮 hover 态 / 按钮手型光标
- 问候语移到消息流上方（原先排在输入框下面，顺序错乱）

v0.6 体验修正：
- 气泡式消息（用户右/助手左，圆角气泡）+ 时间戳
- 打开时只加载最近 10 条历史，自动定位到最新消息
- 统计/周报改为独立弹窗（不混入聊天流）
- ✕ 按钮与 Esc 关闭；聊天中联动机器人状态灯
"""
import html as html_lib
import re
from typing import ClassVar

import markdown as md_lib
import theme
from api_client import ApiClient
from chat_workers import (
    _ApiWorker,
    _ChatWorker,
    _ExecResultWorker,
    _GreetingWorker,
    _HistoryWorker,
    retire,
)
from PySide6.QtCore import QEvent, QSettings, Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QApplication,
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
        browser.setStyleSheet(
            f"background: {theme.token('input_bg')}; color: {theme.token('text_main')};"
            "border: none; font-size: 13px;"
        )
        lay.addWidget(browser, 1)
        close_btn = QPushButton("关闭（Esc）")
        close_btn.clicked.connect(self.close)
        close_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.token('btn_bg')}; color: {theme.token('text_sub')}; border: 1px solid {theme.token('border')};"
            f"border-radius: 8px; padding: 8px; }}"
            f"QPushButton:hover {{ color: #fff; }}"
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


class _ResizeHandle(QWidget):
    """边缘缩放把手：无边框窗口的确定性缩放方案。

    把手是不参与布局的透明小部件，压在卡片外圈的不透明像素上——
    与按钮可点击同一原理，必然能收到鼠标事件，不依赖 Windows
    对分层窗口透明区域的命中测试行为，也不依赖系统缩放调用。
    """

    def __init__(self, parent: QWidget, edges: int, cursor: Qt.CursorShape) -> None:
        super().__init__(parent)
        self._edges = edges
        self._panel: ChatPanel = parent  # type: ignore[assignment]
        self.setCursor(cursor)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._panel.begin_resize(self._edges, event)
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        self._panel.drag_resize(event)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self._panel.end_resize()
        event.accept()


class ChatPanel(QWidget):
    W, H = 460, 600          # 默认尺寸（可缩放，记忆在 QSettings）
    MIN_W, MIN_H = 360, 460  # 缩放下限
    _EDGE = 6                # 边缘缩放热区宽度（px）

    # 边缘组合 → 光标形状（键用 Edge 枚举值的整数位组合）
    _CURSORS: ClassVar[dict[int, Qt.CursorShape]] = {
        Qt.Edge.LeftEdge.value: Qt.CursorShape.SizeHorCursor,
        Qt.Edge.RightEdge.value: Qt.CursorShape.SizeHorCursor,
        Qt.Edge.TopEdge.value: Qt.CursorShape.SizeVerCursor,
        Qt.Edge.BottomEdge.value: Qt.CursorShape.SizeVerCursor,
        Qt.Edge.LeftEdge.value | Qt.Edge.TopEdge.value: Qt.CursorShape.SizeFDiagCursor,
        Qt.Edge.RightEdge.value | Qt.Edge.BottomEdge.value: Qt.CursorShape.SizeFDiagCursor,
        Qt.Edge.RightEdge.value | Qt.Edge.TopEdge.value: Qt.CursorShape.SizeBDiagCursor,
        Qt.Edge.LeftEdge.value | Qt.Edge.BottomEdge.value: Qt.CursorShape.SizeBDiagCursor,
    }

    def __init__(self, ball=None) -> None:
        super().__init__()
        # Window + MinMaxButtonsHint：给原生窗口 WS_THICKFRAME 样式——
        # 没有它 startSystemResize/Move 在 Windows 上会静默无效
        self.setWindowFlags(
            Qt.Window | Qt.FramelessWindowHint | Qt.WindowMinMaxButtonsHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(self.MIN_W, self.MIN_H)
        self._settings = QSettings("PersonalAI", "Assistant")
        self._saved_w = max(self.MIN_W, int(self._settings.value("panel_w", self.W) or self.W))
        self._saved_h = max(self.MIN_H, int(self._settings.value("panel_h", self.H) or self.H))
        self.setMouseTracking(True)  # 悬停边缘时给缩放光标反馈
        self.setFocusPolicy(Qt.StrongFocus)  # 无边框窗口需要显式焦点策略才能收 Esc
        self.client = ApiClient()
        self._worker = None
        self._history_worker = None
        self._api_worker = None
        self._pinned = False
        self.ball = ball  # 悬浮机器人引用：聊天时联动状态灯/表情
        self._history_loaded = False
        self._title: QLabel | None = None
        # 手动缩放/移动兜底（startSystem* 返回 False 时用）
        self._manual_edges = 0
        self._manual_geo = None
        self._manual_pos = None
        self._moving = False
        self._move_offset = None
        # 手动最大化状态（不用原生 isMaximized/showMaximized——原生最大化
        # 对无边框半透明窗口有崩溃风险，状态自管）
        self._maximized = False
        self._pre_max_geo = None
        self._size_save_timer = QTimer(self)
        self._size_save_timer.setSingleShot(True)
        self._size_save_timer.setInterval(400)  # 防抖：拖拽结束才写 QSettings
        self._size_save_timer.timeout.connect(self._save_size)
        self._init_ui()
        # 在 _init_ui 之后 resize：resizeEvent 会布局把手/气泡，需要 UI 就绪
        self.resize(self._saved_w, self._saved_h)

    def paintEvent(self, event) -> None:
        """1/255 透明底漆：圆角外的窗角仍可命中（否则该处点击穿透）。
        边缘缩放不依赖它——把手压在卡片不透明像素上。"""
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 1))

    # ── UI 构建 ────────────────────────────────────────────

    def _init_ui(self) -> None:
        # 卡片铺满窗口（不透明像素直达窗缘）——缩放把手压在卡片外圈上，
        # 依赖"不透明像素必然可命中"，不再依赖任何系统命中测试行为
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.card = card = QWidget()
        card.setObjectName("card")
        # 不透明卡片：背后窗口内容不会再透进来造成"叠字"（样式见 _init_static_styles）
        outer.addWidget(card)

        # 八方向缩放把手：贴边透明小部件，叠在卡片外圈不透明像素上
        # （与按钮可点击同一原理，必然能收到鼠标事件）
        E = Qt.Edge
        self._handles = [
            _ResizeHandle(self, E.LeftEdge.value, Qt.CursorShape.SizeHorCursor),
            _ResizeHandle(self, E.RightEdge.value, Qt.CursorShape.SizeHorCursor),
            _ResizeHandle(self, E.TopEdge.value, Qt.CursorShape.SizeVerCursor),
            _ResizeHandle(self, E.BottomEdge.value, Qt.CursorShape.SizeVerCursor),
            _ResizeHandle(self, E.LeftEdge.value | E.TopEdge.value, Qt.CursorShape.SizeFDiagCursor),
            _ResizeHandle(self, E.RightEdge.value | E.BottomEdge.value, Qt.CursorShape.SizeFDiagCursor),
            _ResizeHandle(self, E.RightEdge.value | E.TopEdge.value, Qt.CursorShape.SizeBDiagCursor),
            _ResizeHandle(self, E.LeftEdge.value | E.BottomEdge.value, Qt.CursorShape.SizeBDiagCursor),
        ]

        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 10, 14, 10)

        # 标题行 + 图钉 + 关闭按钮（按住标题可拖动窗口，双击最大化）
        title = QLabel("🤖 Personal AI Assistant v4.9")
        title.setToolTip("按住拖动窗口 · 双击最大化/还原")
        title.installEventFilter(self)
        self._title = title
        pin_btn = QPushButton("📌")
        pin_btn.setFixedSize(26, 26)
        pin_btn.setCursor(Qt.PointingHandCursor)
        pin_btn.setToolTip("钉住窗口（始终置顶）")
        pin_btn.setCheckable(True)
        pin_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {theme.token('text_faint')}; border: none; font-size: 14px; }}"
            f"QPushButton:hover {{ color: #fff; background: {theme.token('hover')}; border-radius: 13px; }}"
            f"QPushButton:checked {{ color: {theme.token('state')['thinking']}; background: {theme.token('hover')}; border-radius: 13px; }}"
        )
        pin_btn.clicked.connect(self._toggle_pin)
        self._pin_btn = pin_btn
        max_btn = QPushButton("□")
        max_btn.setFixedSize(26, 26)
        max_btn.setCursor(Qt.PointingHandCursor)
        max_btn.setToolTip("最大化/还原")
        max_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {theme.token('text_faint')}; border: none; font-size: 13px; }}"
            f"QPushButton:hover {{ color: #fff; background: {theme.token('hover')}; border-radius: 13px; }}"
        )
        max_btn.clicked.connect(self._toggle_maximize)
        self._max_btn = max_btn
        close_btn = QPushButton("✕")
        self._max_btn_ref = max_btn
        self._close_btn_ref = close_btn
        close_btn.setFixedSize(26, 26)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setToolTip("关闭（Esc）")
        close_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {theme.token('text_faint')}; border: none; font-size: 15px; }}"
            f"QPushButton:hover {{ color: #fff; background: {theme.token('hover')}; border-radius: 13px; }}"
        )
        close_btn.clicked.connect(self.hide)
        title_row = QHBoxLayout()
        title_row.addWidget(title, 1)
        title_row.addWidget(pin_btn)
        title_row.addWidget(max_btn)
        title_row.addWidget(close_btn)
        layout.addLayout(title_row)

        # 问候标签（个性化+时效，每次打开面板刷新；放在消息流上方）
        self._greeting_label = QLabel("你好，我是你的个人助手")
        self._greeting_label.setWordWrap(True)
        layout.addWidget(self._greeting_label)

        # 消息区：滚动容器 + 垂直布局（每条消息 = 一行部件，左右由布局保证）
        self._avatars: list[RobotAvatar] = []   # 所有小月头像（统一动画，防内存泄漏引用）
        self._typing_row: QWidget | None = None  # "正在想"临时行（带思考动画头像）
        self._msg_container = QWidget()
        self._msg_container.setStyleSheet("background: transparent;")  # viewport 不透明会盖住深色卡片
        # 消息区显式 Arrow：防止把手的缩放光标经父子继承链泄漏到内容区
        # （setCursor 沿父链继承，子部件不显式声明就会吃到祖先/邻居的光标）
        self._msg_container.setCursor(Qt.CursorShape.ArrowCursor)
        self._msg_layout = QVBoxLayout(self._msg_container)
        self._msg_layout.setContentsMargins(0, 0, 0, 0)
        self._msg_layout.setSpacing(0)
        self._msg_layout.addStretch(1)  # 底部弹簧：消息从顶部往下排
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._msg_container)
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
            b.setProperty("quick", True)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(
                f"QPushButton {{ background: {theme.token('btn_bg')}; color: {theme.token('text_sub')}; border: 1px solid {theme.token('border')};"
                f"border-radius: 8px; padding: 4px 10px; font-size: 12px; }}"
                f"QPushButton:hover {{ color: {theme.token('text_main')}; border-color: {theme.token('scrollbar')}; background: {theme.token('hover')}; }}"
                f"QPushButton:pressed {{ background: {theme.token('pressed')}; }}"
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
        self.input.setCursor(Qt.CursorShape.IBeamCursor)  # 显式 I-beam（同气泡）
        # 输入框/滚动条/问候样式统一在 _init_static_styles（主题切换可重建）
        self.input.returnPressed.connect(self._send)
        send_btn = QPushButton("发送")
        send_btn.setCursor(Qt.PointingHandCursor)
        send_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.token('accent')}; color: {theme.accent_text()}; border: none;"
            f"border-radius: 8px; padding: 0 18px; min-height: 30px; font-size: 13px; }}"
            f"QPushButton:hover {{ background: {theme.token('accent_hover')}; }}"
            f"QPushButton:pressed {{ background: {theme.token('accent_pressed')}; }}"
        )
        self.send_btn = send_btn
        send_btn.clicked.connect(self._send)
        row.addWidget(self.input, 1)
        row.addWidget(send_btn)
        layout.addLayout(row)

        self._init_static_styles()
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
        self._layout_handles()  # 隐藏期间 resize 事件不投递，首次显示时补布局
        self.input.setFocus()
        self._refresh_greeting()

    # ── 窗口缩放 / 移动 / 最大化（纯手动几何，确定性优先）──
    # 不用 startSystemResize/Move：它可能返回"成功"但实际不动作
    # （FramelessWindowHint 下原生样式被剥离），所以全部本地几何计算。

    def _edge_at(self, pos) -> int:
        """光标位于哪几条边缘热区（外圈 6px，含把手与卡片外环），返回 Edge 位组合。"""
        m = self._EDGE
        r = self.rect()
        e = 0
        if pos.x() <= m:
            e |= Qt.Edge.LeftEdge.value
        if pos.x() >= r.right() - m:
            e |= Qt.Edge.RightEdge.value
        if pos.y() <= m:
            e |= Qt.Edge.TopEdge.value
        if pos.y() >= r.bottom() - m:
            e |= Qt.Edge.BottomEdge.value
        return e

    def begin_resize(self, edges: int, event) -> None:
        """缩放起点（由 _ResizeHandle 或边缘按下触发）。"""
        self._manual_edges = edges
        self._manual_geo = self.geometry()
        self._manual_pos = event.globalPosition().toPoint()

    def drag_resize(self, event) -> None:
        if self._manual_edges:
            self._apply_manual_resize(event.globalPosition().toPoint())

    def end_resize(self) -> None:
        self._manual_edges = 0
        self._save_size()

    def _layout_handles(self) -> None:
        """把手贴到窗缘（叠在卡片外圈不透明像素上）。"""
        m = self._EDGE
        w, h = self.width(), self.height()
        hs = self._handles
        hs[0].setGeometry(0, m, m, h - 2 * m)            # 左
        hs[1].setGeometry(w - m, m, m, h - 2 * m)        # 右
        hs[2].setGeometry(m, 0, w - 2 * m, m)            # 上
        hs[3].setGeometry(m, h - m, w - 2 * m, m)        # 下
        hs[4].setGeometry(0, 0, m, m)                    # 左上
        hs[5].setGeometry(w - m, h - m, m, m)            # 右下
        hs[6].setGeometry(w - m, 0, m, m)                # 右上
        hs[7].setGeometry(0, h - m, m, m)                # 左下

    def mousePressEvent(self, event) -> None:
        # 缩放完全由 _ResizeHandle 接管；面板自身不再处理——
        # 此前的"边缘 6px 判定 + setCursor"会把光标改成缩放箭头、
        # 吞掉气泡文本选择（setCursor 优先级高于子部件的 I-beam）。
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._manual_edges:
            # 缩放进行中（把手发起），跟随更新几何
            if event.buttons() & Qt.LeftButton:
                self._apply_manual_resize(event.globalPosition().toPoint())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._manual_edges:
            self.end_resize()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _apply_manual_resize(self, global_pos) -> None:
        """手动缩放：按按下时的几何 + 位移计算新窗口矩形（含最小尺寸钳制）。"""
        edges = self._manual_edges
        geo = self._manual_geo
        d = global_pos - self._manual_pos
        x, y, w, h = geo.x(), geo.y(), geo.width(), geo.height()
        L, R = Qt.Edge.LeftEdge.value, Qt.Edge.RightEdge.value
        T, B = Qt.Edge.TopEdge.value, Qt.Edge.BottomEdge.value
        if edges & R:
            w = max(self.MIN_W, geo.width() + d.x())
        if edges & B:
            h = max(self.MIN_H, geo.height() + d.y())
        if edges & L:
            w = max(self.MIN_W, geo.width() - d.x())
            x = geo.x() + geo.width() - w
        if edges & T:
            h = max(self.MIN_H, geo.height() - d.y())
            y = geo.y() + geo.height() - h
        self.setGeometry(x, y, w, h)

    def eventFilter(self, obj, event) -> bool:
        """标题栏：按住拖动移动窗口（手动），双击最大化/还原。"""
        if obj is self._title:
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                if self._maximized:
                    return True  # 最大化状态下不允许拖动（先还原再拖）
                self._moving = True
                self._move_offset = (
                    event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                )
                return True
            if event.type() == QEvent.MouseMove and self._moving:
                self.move(event.globalPosition().toPoint() - self._move_offset)
                return True
            if event.type() == QEvent.MouseButtonRelease and self._moving:
                self._moving = False
                return True
            if event.type() == QEvent.MouseButtonDblClick and event.button() == Qt.LeftButton:
                self._toggle_maximize()
                return True
        return super().eventFilter(obj, event)

    def _toggle_maximize(self) -> None:
        """最大化/还原：纯手动几何，不走原生 showMaximized。

        无边框 + 半透明窗口的原生最大化在 Windows 上会让 Qt 原生层崩溃
        （进程无声消失、日志戛然而止——faulthandler 前时代 13:56 的死亡
        模式），改用自己算工作区矩形，零原生调用零风险。
        """
        if self._maximized:
            self.setGeometry(self._pre_max_geo)
            self._maximized = False
        else:
            self._pre_max_geo = self.geometry()
            self._maximized = True
            self.setGeometry(
                QApplication.primaryScreen().availableGeometry()
            )
        self._update_max_btn()

    def _update_max_btn(self) -> None:
        """按钮图标随窗口状态切换：□ 最大化 / ▣ 还原。"""
        self._max_btn.setText("▣" if self._maximized else "□")
        self._max_btn.setToolTip("还原窗口" if self._maximized else "最大化")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout_handles()
        self._apply_bubble_widths()
        if not self._maximized:
            self._size_save_timer.start()

    def hideEvent(self, event) -> None:
        self._save_size()
        super().hideEvent(event)

    def apply_theme(self) -> None:
        """主题切换后重建全部运行时样式（静态样式在 _init_ui 已按 token 构建）。

        动态样式 = 每条消息气泡/时间戳/typing 行（append 时取色）；
        静态样式（卡片/按钮/滚动条/输入框）用 setStyleSheet 重新套一遍。
        """
        self._init_static_styles()
        # 已有消息气泡颜色跟随：按 role 标记重刷（bubble 无 role 属性，直接全量重刷）
        self._retheme_existing_bubbles()

    def _init_static_styles(self) -> None:
        """静态样式构建（_init_ui 与 apply_theme 共用）。"""
        self.card.setStyleSheet(
            f"#card {{ background: {theme.token('card')}; border-radius: 14px; }}"
            f"QLabel {{ color: {theme.token('text_main')}; }}"
        )
        self._greeting_label.setStyleSheet(
            f"QLabel {{ color: {theme.token('text_sub')}; font-size: 12px; padding: 6px 2px;"
            f"border-bottom: 1px solid {theme.token('btn_bg')}; }}"
        )
        self._msg_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { background: transparent; width: 8px; margin: 2px 2px 2px 0; }"
            f"QScrollBar::handle:vertical {{ background: {theme.token('scrollbar')}; border-radius: 4px; min-height: 30px; }}"
            f"QScrollBar::handle:vertical:hover {{ background: {theme.token('hover')}; }}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }"
        )
        self.input.setStyleSheet(
            f"QLineEdit {{ background: {theme.token('input_bg')}; color: {theme.token('text_main')}; border: 1px solid {theme.token('border')};"
            f"border-radius: 8px; padding: 8px; }}"
            f"QLineEdit:focus {{ border: 1px solid {theme.token('accent')}; }}"
        )
        for b in (self._pin_btn, self._max_btn_ref, self._close_btn_ref):
            size_fs = {"📌": 14, "□": 13, "✕": 15}.get(b.text(), 13)
            extra = (
                f"QPushButton:checked {{ color: {theme.token('state')['thinking']}; background: {theme.token('hover')}; border-radius: 13px; }}"
                if b is self._pin_btn else ""
            )
            b.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {theme.token('text_faint')}; border: none; font-size: {size_fs}px; }}"
                f"QPushButton:hover {{ color: #fff; background: {theme.token('hover')}; border-radius: 13px; }}"
                + extra
            )
        for b in self.findChildren(QPushButton):
            if b.property("quick"):
                b.setStyleSheet(
                    f"QPushButton {{ background: {theme.token('btn_bg')}; color: {theme.token('text_sub')}; border: 1px solid {theme.token('border')};"
                    f"border-radius: 8px; padding: 4px 10px; font-size: 12px; }}"
                    f"QPushButton:hover {{ color: {theme.token('text_main')}; border-color: {theme.token('scrollbar')}; background: {theme.token('hover')}; }}"
                    f"QPushButton:pressed {{ background: {theme.token('pressed')}; }}"
                )
        self.send_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.token('accent')}; color: {theme.accent_text()}; border: none;"
            f"border-radius: 8px; padding: 0 18px; min-height: 30px; font-size: 13px; }}"
            f"QPushButton:hover {{ background: {theme.token('accent_hover')}; }}"
            f"QPushButton:pressed {{ background: {theme.token('accent_pressed')}; }}"
        )

    def _retheme_existing_bubbles(self) -> None:
        """已渲染消息按 role 重着色（row 存 role 属性标记）。"""
        for i in range(self._msg_layout.count()):
            row = self._msg_layout.itemAt(i).widget()
            if row is None:
                continue
            role = row.property("msg_role")
            if not role:
                continue
            for lab in row.findChildren(QLabel):
                if lab.property("bubble"):
                    bg = theme.token("bubble_me") if role == "user" else theme.token("bubble_ai")
                    fg = theme.accent_text() if role == "user" else theme.token("text_main")
                    lab.setStyleSheet(
                        f"QLabel {{ background: {bg}; color: {fg}; border-radius: 12px; font-size: 13px; }}"
                    )
                elif lab.property("ts"):
                    lab.setStyleSheet(f"QLabel {{ color: {theme.token('text_faint')}; font-size: 10px; }}")

    def _save_size(self) -> None:
        if self._maximized:
            return  # 最大化尺寸不是常规尺寸，下次打开应还原拖拽后的大小
        self._settings.setValue("panel_w", self.width())
        self._settings.setValue("panel_h", self.height())

    # ── 气泡宽度自适应 ─────────────────────────────────────

    def _bubble_max_width(self) -> int:
        return max(280, int(self._msg_scroll.viewport().width() * 0.72))

    def _apply_bubble_widths(self) -> None:
        """窗口变宽/变窄时同步所有气泡的宽度上限（按 property 找，免记引用）。"""
        max_w = self._bubble_max_width()
        for i in range(self._msg_layout.count()):
            row = self._msg_layout.itemAt(i).widget()
            if row is None:
                continue
            for lab in row.findChildren(QLabel):
                if lab.property("bubble"):
                    lab.setMaximumWidth(max_w)

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
            retire(worker)  # wait 收尸后销毁（防 sizedFree 堆损坏崩溃）
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
            retire(worker)  # wait 收尸后销毁（防 sizedFree 堆损坏崩溃）
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
            retire(worker)  # wait 收尸后销毁（防 sizedFree 堆损坏崩溃）
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
        row.setProperty("msg_role", role)  # 主题切换时按 role 重着色
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 3, 0, 3)
        lay.setSpacing(8)

        if role == "user":
            bg, fg = theme.token("bubble_me"), theme.accent_text()
            align_side = Qt.AlignRight
            lay.addStretch(1)
        else:
            bg, fg = theme.token("bubble_ai"), theme.token("text_main")
            align_side = Qt.AlignLeft
            avatar = RobotAvatar()
            self._avatars.append(avatar)
            lay.addWidget(avatar, 0, Qt.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(2)
        bubble = QLabel(rendered)
        bubble.setWordWrap(True)
        bubble.setTextFormat(Qt.RichText)
        bubble.setProperty("bubble", True)  # 缩放窗口时按此标记统一改宽
        bubble.setMaximumWidth(self._bubble_max_width())
        bubble.setContentsMargins(10, 7, 10, 7)
        bubble.setTextInteractionFlags(Qt.TextSelectableByMouse)  # 支持选中复制
        bubble.setCursor(Qt.CursorShape.IBeamCursor)  # 显式声明：可选中文本区
        bubble.setStyleSheet(
            f"QLabel {{ background: {bg}; color: {fg}; border-radius: 12px; font-size: 13px; }}"
        )
        col.addWidget(bubble, 0, align_side)
        if ts:
            ts_label = QLabel(_fmt_ts(ts))
            ts_label.setProperty("ts", True)
            ts_label.setStyleSheet(f"QLabel {{ color: {theme.token('text_faint')}; font-size: 10px; }}")
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
        bubble.setProperty("bubble", True)
        bubble.setMaximumWidth(self._bubble_max_width())
        bubble.setContentsMargins(14, 7, 14, 7)
        bubble.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bubble.setStyleSheet(
            f"QLabel {{ background: {theme.token('bubble_ai')}; color: {theme.token('text_main')}; border-radius: 12px; font-size: 13px; }}"
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
            retire(worker)  # wait 收尸后销毁
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
            retire(worker)  # wait 收尸后销毁（防 sizedFree 堆损坏崩溃）
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
