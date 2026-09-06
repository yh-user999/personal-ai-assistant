"""无边框窗口的缩放/移动/最大化几何逻辑（自 chat_panel.ChatPanel 迁出）。

FramelessResizeMixin 只承载"纯手动几何"的窗口管理：不用
startSystemResize/Move（FramelessWindowHint 下可能返回"成功"但不动作），
全部本地计算。使用方（ChatPanel）需提供以下属性/方法（鸭子类型）：

- 常量：W / H / MIN_W / MIN_H / _EDGE
- 状态：_manual_edges / _manual_geo / _manual_pos / _moving / _move_offset /
  _maximized / _pre_max_geo / _saved_w / _saved_h
- 部件：_handles（八方向 _ResizeHandle）/ _title / _max_btn / _size_save_timer
- 回调：_save_size()（end_resize 的收尾）
"""
from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import QApplication


class FramelessResizeMixin:
    """无边框窗口几何行为 mixin。事件方法（mouse*/eventFilter）由 Qt 派发到
    ChatPanel 时经 MRO 命中本实现；纯几何方法由把手/按钮回调触发。"""

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

    def _available_screen_geometry(self) -> QRect | None:
        """返回当前窗口所在屏幕的可用工作区，必要时回退到主屏。"""
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return None
        geometry = screen.availableGeometry()
        return QRect(geometry)

    def _cancel_window_interaction(self) -> None:
        """取消进行中的缩放/移动，避免状态切换后继续改写窗口几何。"""
        self._manual_edges = 0
        self._manual_geo = None
        self._manual_pos = None
        self._moving = False
        self._move_offset = None
        self._size_save_timer.stop()

    def _fallback_normal_geometry(self, work_area: QRect | None) -> QRect:
        """根据已记忆尺寸构造可见的常规窗口矩形。"""
        width = max(self.MIN_W, int(getattr(self, "_saved_w", self.W)))
        height = max(self.MIN_H, int(getattr(self, "_saved_h", self.H)))
        if work_area is None or not work_area.isValid():
            return QRect(0, 0, width, height)
        width = min(width, work_area.width())
        height = min(height, work_area.height())
        x = work_area.x() + max(0, (work_area.width() - width) // 2)
        y = work_area.y() + max(0, (work_area.height() - height) // 2)
        return QRect(x, y, width, height)

    def _normal_restore_geometry(self, saved: QRect | None) -> QRect:
        """校验并钳制还原矩形，确保常规窗口尺寸合法且仍在可见工作区。"""
        work_area = self._available_screen_geometry()
        if not isinstance(saved, QRect):
            return self._fallback_normal_geometry(work_area)

        candidate = QRect(saved)
        if (
            not candidate.isValid()
            or candidate.width() < self.MIN_W
            or candidate.height() < self.MIN_H
        ):
            return self._fallback_normal_geometry(work_area)
        if work_area is None or not work_area.isValid():
            return candidate

        width = min(candidate.width(), work_area.width())
        height = min(candidate.height(), work_area.height())
        max_x = work_area.right() - width + 1
        max_y = work_area.bottom() - height + 1
        x = min(max(candidate.x(), work_area.x()), max_x)
        y = min(max(candidate.y(), work_area.y()), max_y)
        return QRect(x, y, width, height)

    def begin_resize(self, edges: int, event) -> None:
        """缩放起点（由 _ResizeHandle 或边缘按下触发）。"""
        if self._maximized:
            return
        self._manual_edges = edges
        self._manual_geo = QRect(self.geometry())
        self._manual_pos = event.globalPosition().toPoint()

    def drag_resize(self, event) -> None:
        if self._maximized:
            self._manual_edges = 0
            return
        if self._manual_edges:
            self._apply_manual_resize(event.globalPosition().toPoint())

    def end_resize(self) -> None:
        self._manual_edges = 0
        self._manual_geo = None
        self._manual_pos = None
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
        if self._maximized:
            return
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
        from PySide6.QtCore import QEvent

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
                self._moving = False
                self._move_offset = None
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
            self._cancel_window_interaction()
            restore_geo = self._normal_restore_geometry(self._pre_max_geo)
            self._maximized = False
            self.setGeometry(restore_geo)
            self._pre_max_geo = None
        else:
            normal_geo = QRect(self.geometry())
            self._cancel_window_interaction()
            self._pre_max_geo = normal_geo
            self._maximized = True
            work_area = self._available_screen_geometry()
            if work_area is not None and work_area.isValid():
                self.setGeometry(work_area)
        self._update_max_btn()

    def _update_max_btn(self) -> None:
        """按钮图标随窗口状态切换：□ 最大化 / ▣ 还原。"""
        self._max_btn.setText("▣" if self._maximized else "□")
        self._max_btn.setToolTip("还原窗口" if self._maximized else "最大化")
