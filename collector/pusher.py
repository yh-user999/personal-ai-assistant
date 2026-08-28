"""事件推送器：线程安全缓冲 + 批量推送 + 断网落盘重试 + 心跳上报。

v0.2 重构要点：
- 修复线程安全 bug：采集在 asyncio.to_thread 工作线程中产生事件，
  旧实现用 get_event_loop().call_soon_threadsafe() 在 Python 3.10+ 会失败；
  现改为 queue.Queue（线程安全）+ 异步消费协程。
- 新增心跳：每 5 分钟上报各通道最近成功时间，服务器可检测采集停滞。
- 新增隐私过滤：事件入队前本地脱敏（可配置）。
"""
import asyncio
import json
import logging
import queue
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger("collector.pusher")

try:
    from privacy_filter import sanitize_event
except ImportError:  # 测试环境兜底
    def sanitize_event(e):
        return e


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventPusher:
    def __init__(self, server_url: str, token: str = "", batch_size: int = 50,
                 flush_interval: float = 30.0, heartbeat_interval: float = 300.0,
                 privacy_filter: bool = True, cache_dir: str = "./cache"):
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.heartbeat_interval = heartbeat_interval
        self.privacy_filter = privacy_filter
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # 线程安全队列：任意线程 put，消费协程 get
        self._queue: queue.Queue = queue.Queue(maxsize=10000)
        # 各采集通道最近成功时间（通道名 → ISO 时间）；GIL 下单键赋值线程安全
        self._health: dict = {}
        self._running = True
        self._client: httpx.AsyncClient | None = None  # 长驻客户端，避免每批次重建

    # ── 生产者侧（任意线程调用，同步）────────────────────────

    def add_event(self, event: dict) -> None:
        """入队。事件在入队前完成本地脱敏。"""
        if self.privacy_filter:
            event = sanitize_event(event)
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            # 背压：队列满则落盘，防止内存无限增长
            self._save_to_disk([event])

    def report_health(self, channel: str, ts: str = None) -> None:
        """采集通道报告心跳（成功完成一轮采集时调用）。"""
        self._health[channel] = ts or _now_iso()

    def flush_to_disk(self) -> None:
        """停止前把内存队列剩余事件落盘（下次启动时重试推送）。"""
        batch = self._drain(batch_size=10_000)
        if batch:
            self._save_to_disk(batch)

    # ── 消费侧（异步）──────────────────────────────────────

    async def run(self) -> None:
        """消费循环：批量推送。"""
        await self._retry_cache()
        while self._running:
            batch = self._drain(self.batch_size)
            if not batch:
                await asyncio.sleep(1)
                continue
            await self._push_batch(batch)

    async def heartbeat(self) -> None:
        """心跳循环：上报各通道最近成功时间。"""
        while self._running:
            await asyncio.sleep(self.heartbeat_interval)
            await self._send_heartbeat()

    async def stop(self) -> None:
        self._running = False
        self.flush_to_disk()
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    # ── 内部实现 ────────────────────────────────────────────

    def _drain(self, batch_size: int) -> list[dict]:
        batch = []
        for _ in range(batch_size):
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10, trust_env=False)
        return self._client

    async def _push_batch(self, batch: list[dict]) -> None:
        try:
            r = await self._get_client().post(
                f"{self.server_url}/api/events",
                json={"events": batch},
                headers=self._headers(),
            )
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            logger.info("推送成功 %d 条", len(batch))
        except Exception as e:
            logger.warning("推送失败，落盘 %d 条: %s", len(batch), e)
            self._save_to_disk(batch)

    async def _send_heartbeat(self) -> None:
        payload = {"client": "collector", "channels": dict(self._health)}
        try:
            r = await self._get_client().post(
                f"{self.server_url}/api/heartbeat",
                json=payload,
                headers=self._headers(),
            )
            if r.status_code == 200:
                logger.info("心跳已发送: %s", payload["channels"])
            else:
                logger.warning("心跳响应异常 HTTP %s", r.status_code)
        except Exception as e:
            # 心跳失败不致命（下轮重试），但必须留痕——静默失败无法排障
            logger.warning("心跳发送失败: %s", e)

    async def _retry_cache(self) -> None:
        """启动时推送历史落盘缓存。"""
        for f in sorted(self.cache_dir.glob("pending_*.jsonl")):
            events = []
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            if events:
                await self._push_batch(events)
            f.unlink(missing_ok=True)

    def _save_to_disk(self, events: list[dict]) -> None:
        f = self.cache_dir / f"pending_{int(time.time())}.jsonl"
        with f.open("a", encoding="utf-8") as fp:
            for ev in events:
                fp.write(json.dumps(ev, ensure_ascii=False) + "\n")
