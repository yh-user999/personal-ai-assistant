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
                 snapshot_interval: float = 30.0,
                 privacy_filter: bool = True, cache_dir: str = "./cache"):
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.snapshot_interval = snapshot_interval
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
        # 通道停滞检测：各通道预期间隔 + 已告警标记（避免同一次停滞反复告警）
        self._channel_interval: dict[str, float] = {}
        self._stalled: set[str] = set()
        self._stop_event: asyncio.Event | None = None

    # ── 生产者侧（任意线程调用，同步）────────────────────────

    def add_event(self, event: dict) -> None:
        """入队。事件在入队前完成本地脱敏。"""
        if self.privacy_filter:
            event = sanitize_event(event)
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            # 背压：队列满则落盘，防止内存无限增长；磁盘满也不能杀死采集器。
            try:
                self._save_to_disk([event])
            except OSError as e:
                logger.error("队列满且落盘失败，丢弃 1 条事件: %s", e)

    def report_health(self, channel: str, ts: str = None) -> None:
        """采集通道报告心跳（成功完成一轮采集时调用）。

        顺带解除停滞标记：通道恢复后下次停滞仍会重新告警。
        """
        self._health[channel] = ts or _now_iso()
        if channel in self._stalled:
            self._stalled.discard(channel)
            logger.info("采集通道 %s 已恢复", channel)

    def register_channel(self, channel: str, interval: float) -> None:
        """登记通道的预期采集间隔（用于停滞判定）。

        没有这层登记，report_health 上报的时间戳没人消费——通道静默死亡
        （window_monitor 曾是裸 except: pass）时进程还活着，用户毫无感知。
        """
        self._channel_interval[channel] = interval
        self._health.setdefault(channel, _now_iso())

    def flush_to_disk(self) -> None:
        """停止前把内存队列剩余事件落盘（下次启动时重试推送）。"""
        batch = self._drain(batch_size=10_000)
        if batch:
            self._save_to_disk(batch)

    # ── 消费侧（异步）──────────────────────────────────────

    async def run(self) -> None:
        """消费循环：批量推送 + 周期性快照（防断电/强杀丢队列内存事件）。"""
        await self._retry_cache()
        last_snapshot = time.time()
        while self._running:
            batch = self._drain(self.batch_size)
            if not batch:
                # 空转期间每 snapshot_interval 秒把队列镜像落盘一次：
                # 强杀/断电时最多丢一个快照窗口内的入队事件
                if time.time() - last_snapshot >= self.snapshot_interval:
                    self._snapshot_queue()
                    last_snapshot = time.time()
                await asyncio.sleep(1)
                continue
            await self._push_batch(batch)
            last_snapshot = time.time()
            # 推送成功后刷新快照（队列内容已变）
            self._snapshot_queue()

    async def heartbeat(self) -> None:
        """心跳循环：上报各通道最近成功时间 + 检测通道停滞。

        先上报再等待：旧实现循环首行就 sleep(300)，启动后 5 分钟内服务器看不到
        任何心跳，会误判"采集停滞"（chat 的电脑在线判定就依赖这个）。
        用 Event.wait 代替 sleep：stop() 后立即退出，不必等满一个周期。
        """
        if self._stop_event is None:
            self._stop_event = asyncio.Event()
        while self._running:
            await self._send_heartbeat()
            self._check_stalled_channels()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.heartbeat_interval
                )
                return  # 收到停止信号
            except asyncio.TimeoutError:
                continue  # 正常到期，继续下一轮

    def _check_stalled_channels(self) -> None:
        """通道超过预期间隔 3 倍没产出数据 → 上报一条 collector_alert 事件。

        走事件通道而不是只记日志：机器人能据此提示"浏览器采集已停 30 分钟"，
        否则用户只能靠翻日志发现通道死了。
        """
        now = datetime.now(timezone.utc)
        for channel, interval in self._channel_interval.items():
            last = self._health.get(channel)
            if not last or channel in self._stalled:
                continue
            try:
                last_dt = datetime.fromisoformat(last)
            except (ValueError, TypeError):
                continue
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            idle = (now - last_dt).total_seconds()
            if idle > interval * 3:
                self._stalled.add(channel)
                mins = int(idle // 60)
                logger.warning("采集通道 %s 已停滞 %d 分钟", channel, mins)
                self.add_event({
                    "kind": "collector_alert",
                    "name": channel,
                    "detail": f"{channel} 采集通道已停滞约 {mins} 分钟（预期每 {int(interval)}s 一轮）",
                    "start_ts": _now_iso(),
                })

    async def stop(self) -> None:
        self._running = False
        if self._stop_event is not None:
            self._stop_event.set()  # 唤醒心跳循环，不必等满一个周期
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

    async def _push_batch(self, batch: list[dict], save_on_fail: bool = True) -> bool:
        """推送一批事件。返回是否成功。

        save_on_fail=False 用于缓存重放——重放失败时不该再落一份新盘文件
        （原文件还在，重复落盘会让缓存文件数在长期断网下线性膨胀）。
        """
        try:
            r = await self._get_client().post(
                f"{self.server_url}/api/events",
                json={"events": batch},
                headers=self._headers(),
            )
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            logger.info("推送成功 %d 条", len(batch))
            return True
        except Exception as e:
            if not save_on_fail:
                logger.warning("重放失败 %d 条（保留缓存文件下次再试）: %s", len(batch), e)
                return False
            logger.warning("推送失败，落盘 %d 条: %s", len(batch), e)
            try:
                self._save_to_disk(batch)
            except OSError as save_error:
                logger.error("推送失败且落盘失败，丢弃 %d 条事件: %s", len(batch), save_error)
            return False

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
        """启动时推送历史落盘缓存（含上次的队列快照）。"""
        files = [
            f for f in sorted(self.cache_dir.glob("pending_*.jsonl"))
            if f.name != self._SNAPSHOT_NAME
        ]
        snapshot = self.cache_dir / self._SNAPSHOT_NAME
        if snapshot.exists():
            files.append(snapshot)  # 快照最后重放（其内容最接近崩溃现场）
        for f in files:
            events = []
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            if not events:
                f.unlink(missing_ok=True)  # 空/全损坏文件直接清掉
                continue
            # 只有推送成功才删文件。旧实现无论成败都 unlink——依赖"失败会重新
            # 落盘"兜底，可是那条路径在磁盘满时只 log.error 就丢事件，
            # 于是断网 + 磁盘紧张会静默丢掉整批历史事件。
            if await self._push_batch(events, save_on_fail=False):
                f.unlink(missing_ok=True)
            else:
                logger.info("缓存 %s 保留，下次启动或恢复网络后重试", f.name)
                break  # 网络不通，后面的文件不必再试

    _SNAPSHOT_NAME = "pending_snapshot.jsonl"

    def _snapshot_queue(self) -> None:
        """把当前内存队列镜像落盘（覆盖写）：强杀/断电后 _retry_cache 重放。

        注意快照与 pending_*.jsonl 的关系：快照包含的条目在重放后从队列
        清除（读后即删），不会双倍重放——queue 消费天然幂等。
        """
        items = list(self._queue.queue)  # deque 快照（不改队列状态）
        f = self.cache_dir / self._SNAPSHOT_NAME
        try:
            if items:
                lines = [json.dumps(ev, ensure_ascii=False) for ev in items]
                f.write_text("\n".join(lines) + "\n", encoding="utf-8")
            elif f.exists():
                f.unlink()  # 队列已清空：陈旧快照作废
        except OSError as e:
            logger.warning("队列快照写盘失败: %s", e)

    def _save_to_disk(self, events: list[dict]) -> None:
        f = self.cache_dir / f"pending_{int(time.time())}.jsonl"
        with f.open("a", encoding="utf-8") as fp:
            for ev in events:
                fp.write(json.dumps(ev, ensure_ascii=False) + "\n")
