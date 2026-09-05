"""采集器可靠性测试：git 游标时区、通道停滞告警、缓存重放不丢数据、配置路径。

这几处此前零覆盖，恰好也是本轮审计发现真实 bug 的地方。
sys.path 由 tests/conftest.py 注入（仓库根 + server/）。
"""
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "collector"))


# ── ① git 游标的时区正确性 ────────────────────────────────

def test_to_dt_parses_offsets():
    from git_scanner import GitScanner

    assert GitScanner._to_dt("") is None
    assert GitScanner._to_dt("garbage") is None
    # 无时区 → 视为 UTC
    assert GitScanner._to_dt("2024-05-01T10:00:00") == datetime(
        2024, 5, 1, 10, tzinfo=timezone.utc
    )
    # 带偏移 → 归一化到 UTC
    assert GitScanner._to_dt("2024-05-01T10:00:00+08:00") == datetime(
        2024, 5, 1, 2, tzinfo=timezone.utc
    )
    assert GitScanner._to_dt("2024-05-01T10:00:00Z") == datetime(
        2024, 5, 1, 10, tzinfo=timezone.utc
    )


def test_cross_timezone_ordering_differs_from_string_compare():
    """这就是旧 bug 的本质：字符串比较与真实时序结论相反。"""
    from git_scanner import GitScanner

    a, b = "2024-05-01T10:00:00+08:00", "2024-05-01T03:30:00+00:00"
    assert a > b                                     # 字符串：a 更大
    assert GitScanner._to_dt(a) < GitScanner._to_dt(b)  # 真实时刻：a 更早


@pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True, check=False).returncode != 0,
    reason="需要 git",
)
def test_cursor_takes_truly_latest_commit(tmp_path):
    """跨时区的两个提交：游标必须落在真实最晚的那个上。

    旧实现按字符串取最大值，会把游标顶到"名义时刻大但真实更早"的提交，
    甚至顶到未来，其间提交永久漏采（游标已落盘，无补采路径）。
    """
    from git_scanner import GitScanner

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args, **env):
        subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True,
            env={**__import__("os").environ, **env},
        )

    git("init", "-q", ".")
    git("config", "user.email", "t@t.com")
    git("config", "user.name", "T")

    now = datetime.now(timezone.utc)
    # 名义时刻大、真实更早（+08:00）
    early = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S") + "+08:00"
    # 名义时刻小、真实更晚（UTC）
    late = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"

    (repo / "a.txt").write_text("a")
    git("add", ".")
    git("commit", "-q", "-m", "east8", f"--date={early}", GIT_COMMITTER_DATE=early)
    (repo / "b.txt").write_text("b")
    git("add", ".")
    git("commit", "-q", "-m", "utc", f"--date={late}", GIT_COMMITTER_DATE=late)

    class FakePusher:
        def __init__(self):
            self.events = []

        def add_event(self, e):
            self.events.append(e)

        def report_health(self, ch):
            pass

    cursor_file = tmp_path / "cursor.json"
    scanner = GitScanner(FakePusher(), repos=str(repo), cursor_file=str(cursor_file))
    scanner._scan_all()

    saved = json.loads(cursor_file.read_text())[str(repo)]
    saved_dt = datetime.fromisoformat(saved)
    assert saved_dt <= datetime.now(timezone.utc), "游标不能被顶到未来"
    # 游标应等于真实最晚提交（UTC 那条），而非字符串最大的那条
    assert saved_dt == GitScanner._to_dt(late)


# ── ② 通道停滞告警 ────────────────────────────────────────

def _make_pusher(tmp_path):
    from pusher import EventPusher

    return EventPusher("http://127.0.0.1:1", cache_dir=str(tmp_path / "cache"))


def test_stalled_channel_emits_alert(tmp_path):
    """通道超过预期间隔 3 倍无产出 → 上报 collector_alert 事件。"""
    p = _make_pusher(tmp_path)
    p.register_channel("browser", 600)
    p._health["browser"] = (
        datetime.now(timezone.utc) - timedelta(minutes=35)
    ).isoformat()

    p._check_stalled_channels()

    events = list(p._queue.queue)
    assert len(events) == 1
    assert events[0]["kind"] == "collector_alert"
    assert "browser" in events[0]["detail"]


def test_stall_alert_not_repeated(tmp_path):
    """同一次停滞只告警一次（否则每个心跳周期轰炸一条）。"""
    p = _make_pusher(tmp_path)
    p.register_channel("browser", 600)
    p._health["browser"] = (
        datetime.now(timezone.utc) - timedelta(minutes=35)
    ).isoformat()
    p._check_stalled_channels()
    p._check_stalled_channels()
    assert p._queue.qsize() == 1


def test_recovery_clears_stall_flag(tmp_path):
    """通道恢复后要能再次告警（否则第二次停滞永远静默）。"""
    p = _make_pusher(tmp_path)
    p.register_channel("browser", 600)
    p._stalled.add("browser")
    p.report_health("browser")
    assert "browser" not in p._stalled


def test_healthy_channel_no_alert(tmp_path):
    p = _make_pusher(tmp_path)
    p.register_channel("window", 8)
    p.report_health("window")
    p._check_stalled_channels()
    assert p._queue.qsize() == 0


# ── ③ 缓存重放不丢数据 ───────────────────────────────────

def test_failed_replay_keeps_cache_file(tmp_path):
    """推送失败时必须保留缓存文件。

    旧实现无论成败都 unlink，依赖"失败会重新落盘"兜底，而那条路径在
    磁盘满时只 log.error 就丢事件——断网 + 磁盘紧张会静默丢整批历史。
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    for name in ("pending_100.jsonl", "pending_200.jsonl"):
        (cache / name).write_text(
            json.dumps({"kind": "app_usage", "name": "x", "detail": "y"}) + "\n",
            encoding="utf-8",
        )

    from pusher import EventPusher

    p = EventPusher("http://127.0.0.1:1", cache_dir=str(cache))  # 故意连不通
    asyncio.run(p._retry_cache())

    remaining = sorted(f.name for f in cache.glob("pending_*.jsonl"))
    assert remaining == ["pending_100.jsonl", "pending_200.jsonl"]


def test_empty_cache_file_removed(tmp_path):
    """空/全损坏的缓存文件直接清掉，不必反复重试。"""
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "pending_1.jsonl").write_text("not json\n", encoding="utf-8")

    from pusher import EventPusher

    p = EventPusher("http://127.0.0.1:1", cache_dir=str(cache))
    asyncio.run(p._retry_cache())
    assert list(cache.glob("pending_*.jsonl")) == []


# ── ④ 配置路径 ───────────────────────────────────────────

def test_collector_env_file_points_to_repo_root():
    """.env 必须解析到仓库根。

    原先写 parents[2]（仓库外层），.env 从未被 pydantic 读到——
    只因 main.py 先跑 load_dotenv 才碰巧生效，单独导入时静默用默认值。
    """
    import config

    expected = Path(config.__file__).resolve().parents[1] / ".env"
    assert config.CollectorSettings.model_config["env_file"] == expected


def test_cache_path_is_absolute_regardless_of_cwd():
    """相对 cache_dir 以 collector/ 为基准，不受进程 CWD 影响。

    开机自启时 CWD 常是 C:\\Windows\\System32，按 CWD 解析会把落盘队列
    与游标写到意外位置甚至失败。
    """
    from config import CollectorSettings

    s = CollectorSettings(cache_dir="./cache")
    assert s.cache_path.is_absolute()
    assert s.cache_path.parent.name == "collector"
