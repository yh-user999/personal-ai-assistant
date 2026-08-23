"""前台窗口监控：Win32 API 轮询当前活动窗口，记录应用使用时长。

实现：ctypes 调 user32.GetForegroundWindow + GetWindowText + 进程名。
无第三方依赖（Windows 平台）。
"""
import asyncio
import ctypes
import time
from datetime import datetime, timezone

from pusher import EventPusher

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _active_window() -> tuple[str, str] | None:
    """返回 (进程名, 窗口标题)，失败返回 None。"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    # 窗口标题
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    title = buf.value.strip()[:120]

    # 进程名
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    proc = ctypes.c_void_p()
    if not kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value):
        return (title or "unknown", title)
    exe_buf = ctypes.create_unicode_buffer(260)
    size = ctypes.c_ulong(260)
    kernel32.QueryFullProcessImageNameW(proc, 0, exe_buf, ctypes.byref(size))
    kernel32.CloseHandle(proc)
    import os
    app = os.path.basename(exe_buf.value) or "unknown"
    return (app, title)


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

    async def stop(self) -> None:
        self._running = False
        self._flush(time.time())
        await self.pusher.flush()
