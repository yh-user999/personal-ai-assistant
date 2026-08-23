"""事件推送器：批量推送行为事件到服务器，断网时本地缓存重试。

- 每满 50 条或每 30s 推送一次
- 推送失败（断网/服务器不可达）→ 事件落盘 cache/pending_*.jsonl，恢复后重试
"""
import asyncio
import json
import time
from pathlib import Path

import httpx


class EventPusher:
    def __init__(self, server_url: str, token: str = "", batch_size: int = 50, flush_interval: float = 30.0):
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self._events: list[dict] = []
        self._lock = asyncio.Lock()
        self._cache_dir = Path("./cache")
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def add_event(self, event: dict) -> None:
        """同步入口（采集线程调用），事件进内存队列。"""
        # 简化：单事件循环下直接用 append（采集器均为 to_thread 内调用，用锁保护）
        asyncio.get_event_loop().call_soon_threadsafe(
            lambda: asyncio.ensure_future(self._add(event))
        )

    async def _add(self, event: dict) -> None:
        async with self._lock:
            self._events.append(event)
            if len(self._events) >= self.batch_size:
                await self._push_batch()

    async def flush(self) -> None:
        async with self._lock:
            await self._push_batch()

    async def _push_batch(self) -> None:
        if not self._events:
            return
        batch, self._events = self._events[: self.batch_size], []
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    f"{self.server_url}/api/events",
                    json={"events": batch},
                    headers=headers,
                )
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
        except Exception as e:
            print(f"[pusher] 推送失败，缓存 {len(batch)} 条: {e}")
            # 落盘缓存
            f = self._cache_dir / f"pending_{int(time.time())}.jsonl"
            with f.open("a", encoding="utf-8") as fp:
                for ev in batch:
                    fp.write(json.dumps(ev, ensure_ascii=False) + "\n")

    async def retry_cache(self) -> None:
        """启动时重试历史缓存。"""
        for f in sorted(self._cache_dir.glob("pending_*.jsonl")):
            events = []
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            if events:
                async with self._lock:
                    self._events.extend(events)
                await self._push_batch()
            f.unlink(missing_ok=True)
