from __future__ import annotations

import sys
from pathlib import Path

import pytest

DESKTOP_DIR = Path(__file__).resolve().parents[1]
if str(DESKTOP_DIR) not in sys.path:
    sys.path.insert(0, str(DESKTOP_DIR))

pytest.importorskip("PySide6")
from PySide6.QtCore import QRect, Qt

import chat_panel


class _FakeScreen:
    def __init__(self, geometry: QRect) -> None:
        self._geometry = QRect(geometry)

    def availableGeometry(self) -> QRect:
        return QRect(self._geometry)


class _FakeTimer:
    def __init__(self) -> None:
        self.stop_count = 0

    def stop(self) -> None:
        self.stop_count += 1


class _FakeButton:
    def __init__(self) -> None:
        self.text = ""
        self.tooltip = ""

    def setText(self, text: str) -> None:
        self.text = text

    def setToolTip(self, text: str) -> None:
        self.tooltip = text


class _FakeSettings:
    def __init__(self) -> None:
        self.writes = []

    def setValue(self, key: str, value: int) -> None:
        self.writes.append((key, value))


class _PanelStub:
    """只提供窗口状态方法所需的最小接口，避免启动真实 UI 或后台 worker。"""

    W = chat_panel.ChatPanel.W
    H = chat_panel.ChatPanel.H
    MIN_W = chat_panel.ChatPanel.MIN_W
    MIN_H = chat_panel.ChatPanel.MIN_H

    _available_screen_geometry = chat_panel.ChatPanel._available_screen_geometry
    _cancel_window_interaction = chat_panel.ChatPanel._cancel_window_interaction
    _fallback_normal_geometry = chat_panel.ChatPanel._fallback_normal_geometry
    _normal_restore_geometry = chat_panel.ChatPanel._normal_restore_geometry
    _toggle_maximize = chat_panel.ChatPanel._toggle_maximize
    _update_max_btn = chat_panel.ChatPanel._update_max_btn
    _save_size = chat_panel.ChatPanel._save_size
    begin_resize = chat_panel.ChatPanel.begin_resize

    def __init__(self, geometry: QRect, screen_geometry: QRect) -> None:
        self._geometry = QRect(geometry)
        self._screen = _FakeScreen(screen_geometry)
        self._saved_w = 520
        self._saved_h = 620
        self._manual_edges = 0
        self._manual_geo = None
        self._manual_pos = None
        self._moving = False
        self._move_offset = None
        self._maximized = False
        self._pre_max_geo = None
        self._size_save_timer = _FakeTimer()
        self._max_btn = _FakeButton()
        self._settings = _FakeSettings()

    def geometry(self) -> QRect:
        return QRect(self._geometry)

    def setGeometry(self, *args) -> None:
        self._geometry = QRect(args[0]) if len(args) == 1 else QRect(*args)

    def screen(self) -> _FakeScreen:
        return self._screen

    def width(self) -> int:
        return self._geometry.width()

    def height(self) -> int:
        return self._geometry.height()


def _inside(area: QRect, geometry: QRect) -> bool:
    return (
        area.left() <= geometry.left()
        and area.top() <= geometry.top()
        and geometry.right() <= area.right()
        and geometry.bottom() <= area.bottom()
    )


def test_normal_geometry_round_trips_after_maximize_and_restore() -> None:
    normal = QRect(140, 180, 520, 680)
    work_area = QRect(0, 0, 1600, 900)
    panel = _PanelStub(normal, work_area)

    panel._toggle_maximize()
    assert panel._maximized is True
    assert panel.geometry() == work_area
    assert panel._pre_max_geo == normal
    assert panel._pre_max_geo is not panel._geometry

    panel._toggle_maximize()
    assert panel._maximized is False
    assert panel.geometry() == normal
    assert panel._pre_max_geo is None
    assert panel._max_btn.text == "□"


def test_repeated_maximize_restore_cycles_keep_normal_geometry() -> None:
    normal = QRect(220, 120, 640, 700)
    work_area = QRect(0, 0, 1920, 1040)
    panel = _PanelStub(normal, work_area)

    for _ in range(3):
        panel._toggle_maximize()
        assert panel.geometry() == work_area
        panel._toggle_maximize()
        assert panel.geometry() == normal
        assert panel._maximized is False


def test_invalid_restore_snapshot_falls_back_to_visible_normal_geometry() -> None:
    work_area = QRect(100, 50, 1200, 800)
    panel = _PanelStub(work_area, work_area)
    panel._maximized = True
    panel._pre_max_geo = None

    panel._toggle_maximize()

    restored = panel.geometry()
    assert panel._maximized is False
    assert panel._pre_max_geo is None
    assert restored.width() == panel._saved_w
    assert restored.height() == panel._saved_h
    assert _inside(work_area, restored)


def test_too_small_restore_snapshot_uses_normal_size_fallback() -> None:
    work_area = QRect(100, 50, 1200, 800)
    panel = _PanelStub(work_area, work_area)
    panel._maximized = True
    panel._pre_max_geo = QRect(0, 0, 100, 100)

    panel._toggle_maximize()

    restored = panel.geometry()
    assert restored.width() == panel._saved_w
    assert restored.height() == panel._saved_h
    assert _inside(work_area, restored)


def test_maximized_window_does_not_save_maximized_size_or_start_resize() -> None:
    normal = QRect(160, 160, 500, 600)
    work_area = QRect(0, 0, 1600, 900)
    panel = _PanelStub(normal, work_area)
    panel._manual_edges = Qt.Edge.RightEdge.value
    panel._manual_geo = QRect(normal)
    panel._moving = True
    panel._move_offset = object()

    panel._toggle_maximize()
    panel._save_size()
    panel.begin_resize(Qt.Edge.RightEdge.value, None)

    assert panel._maximized is True
    assert panel._settings.writes == []
    assert panel._manual_edges == 0
    assert panel._manual_geo is None
    assert panel._manual_pos is None
    assert panel._moving is False
    assert panel._move_offset is None
    assert panel._size_save_timer.stop_count == 1


def test_maximize_uses_current_screen_and_clamps_restore_geometry() -> None:
    secondary = QRect(1920, 40, 1280, 1000)
    panel = _PanelStub(QRect(2100, 200, 480, 560), secondary)

    panel._toggle_maximize()
    assert panel.geometry() == secondary

    panel._pre_max_geo = QRect(3100, 900, 500, 300)
    panel._toggle_maximize()

    restored = panel.geometry()
    assert restored == QRect(2700, 740, 500, 300)
    assert _inside(secondary, restored)
