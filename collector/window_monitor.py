"""前台窗口监控：Win32 API 轮询当前活动窗口，记录应用使用时长。

实现：ctypes 调 user32.GetForegroundWindow + GetWindowText + 进程名。
无第三方依赖（Windows 平台）。

v0.4 修复（来自 Windows 实测）：
- Win32 函数必须显式声明 restype/argtypes——64 位 HANDLE/HWND 若不声明
  会被 ctypes 截断为 32 位，导致 OpenProcess 拿到错句柄 → 进程名全变 unknown
- OpenProcess 对受保护进程（任务管理器等）会 Access Denied：
  降级为从窗口标题提取应用名（"xxx - Notepad" → Notepad）
"""
import asyncio
import ctypes
import os
import sys
import time
from datetime import datetime, timezone

from pusher import EventPusher

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# ── Win32 函数签名（64 位句柄防截断，v0.4 修复）─────────────
# 仅在 Windows 平台加载（Linux 上导入本模块只用于纯函数测试）
if sys.platform == "win32":
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetForegroundWindow.argtypes = []
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]

    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_bool
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32),
    ]
    kernel32.CloseHandle.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
else:
    user32 = kernel32 = None


def guess_app_from_title(title: str) -> str:
    """降级方案：从窗口标题猜应用名。
    "基于QQ机器人… - Google Chrome" → "Google Chrome"
    "任务管理器" → "任务管理器"
    """
    if not title:
        return "unknown"
    if " - " in title:
        return title.rsplit(" - ", 1)[-1].strip()[:60] or "unknown"
    return title.strip()[:60]


def _active_window() -> tuple[str, str] | None:
    """返回 (进程名, 窗口标题)，失败返回 None。仅 Windows 可用。"""
    if user32 is None:
        return None
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    # 窗口标题
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    title = buf.value.strip()[:120]

    # 进程名：OpenProcess → QueryFullProcessImageName
    pid = ctypes.c_uint32()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if proc:
        try:
            exe_buf = ctypes.create_unicode_buffer(260)
            size = ctypes.c_uint32(260)
            if kernel32.QueryFullProcessImageNameW(proc, 0, exe_buf, ctypes.byref(size)):
                app = os.path.basename(exe_buf.value).strip() or "unknown"
                return (app, title)
        finally:
            kernel32.CloseHandle(proc)
    # 受保护进程（任务管理器等）或查询失败 → 从标题猜
    return (guess_app_from_title(title), title)


class WindowMonitor:
    def __init__(self, pusher: EventPusher, interval: float = 8.0):
        self.pusher = pusher
        self.interval = interval
        self._current: tuple[str, str, float] | None = None  # (app, title, start_ts)
        self._running = True

    async def run(self) -> None:
        while self._running:
            try:
                await asyncio.to_thread(self._tick)
                self.pusher.report_health("window")
            except Exception:
                pass
            await asyncio.sleep(self.interval)

    def _tick(self) -> None:
        info = _active_window()
        now = time.time()
        if info is None:
            return
        app, title = info
        # 窗口变化 → 结束上一段，开始新一段
        if self._current and (app, title) != (self._current[0], self._current[1]):
            self._flush(now)
        if self._current is None:
            self._current = (app, title, now)

    def _flush(self, end: float) -> None:
        if not self._current:
            return
        app, title, start = self._current
        duration = end - start
        if duration >= 5:  # 过滤 <5s 的瞬切
            self.pusher.add_event({
                "kind": "app_usage",
                "name": app,
                "detail": title,
                "start_ts": datetime.fromtimestamp(start, timezone.utc).isoformat(),
                "end_ts": datetime.fromtimestamp(end, timezone.utc).isoformat(),
            })
        self._current = None

    def stop(self) -> None:
        self._running = False
        self._flush(time.time())
        self.pusher.flush_to_disk()
