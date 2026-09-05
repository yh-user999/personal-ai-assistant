"""浏览器多 profile / Firefox 采集 + 时段推荐测试。

此前 browser_history 只硬编码了 Chrome/Edge 的 Default profile，
多 profile（工作/个人分离是常见用法）与 Firefox 全部漏采；
launcher 的 use_count 只用于排序，没有任何推荐应用。
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "collector"))

from common import launcher


class FakePusher:
    def __init__(self):
        self.events = []

    def add_event(self, e):
        self.events.append(e)

    def report_health(self, ch):
        pass


def _make_chromium_history(path: Path, title: str, visit_time: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT)")
    conn.execute(
        "CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER)"
    )
    conn.execute("INSERT INTO urls VALUES (1,'https://a.com/x',?)", (title,))
    conn.execute("INSERT INTO visits VALUES (1,1,?)", (visit_time,))
    conn.commit()
    conn.close()


def _make_firefox_places(path: Path, title: str, visit_date: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE moz_places(id INTEGER PRIMARY KEY, url TEXT, title TEXT)")
    conn.execute(
        "CREATE TABLE moz_historyvisits"
        "(id INTEGER PRIMARY KEY, place_id INTEGER, visit_date INTEGER)"
    )
    conn.execute("INSERT INTO moz_places VALUES (1,'https://fx.org/p',?)", (title,))
    conn.execute("INSERT INTO moz_historyvisits VALUES (1,1,?)", (visit_date,))
    conn.commit()
    conn.close()


@pytest.fixture
def browser_env(tmp_path, monkeypatch):
    """搭一套多浏览器多 profile 的目录结构。"""
    local = tmp_path / "Local"
    roaming = tmp_path / "Roaming"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(roaming))

    wk = 13400000000000000  # WebKit 微秒
    _make_chromium_history(
        local / "Google/Chrome/User Data/Default/History", "chrome-default", wk
    )
    _make_chromium_history(
        local / "Google/Chrome/User Data/Profile 1/History", "chrome-p1", wk
    )
    _make_chromium_history(
        local / "Microsoft/Edge/User Data/Default/History", "edge-default", wk
    )
    _make_firefox_places(
        roaming / "Mozilla/Firefox/Profiles/abc.default/places.sqlite",
        "firefox-title",
        1755000000000000,  # Unix 微秒
    )
    return tmp_path


def test_discovers_all_profiles_and_browsers(browser_env):
    import browser_history as bh

    labels = [label for label, _ in bh.discover_chromium_histories()]
    assert "chrome/Default" in labels
    assert "chrome/Profile 1" in labels, "多 profile 曾被漏采"
    assert "edge/Default" in labels

    fx = [label for label, _ in bh.discover_firefox_histories()]
    assert fx == ["firefox/abc.default"]


def test_collects_from_every_source(browser_env, tmp_path):
    """四个来源都要采到（旧实现只采 chrome/Default 一个）。"""
    import browser_history as bh

    pusher = FakePusher()
    c = bh.BrowserHistoryCollector(
        pusher, cursor_file=str(tmp_path / "cursor.json")
    )
    c._default_cursor = 0
    c._cursors = {}
    c._collect_once()

    sources = sorted(e["meta"]["source"] for e in pusher.events)
    assert sources == [
        "chrome/Default", "chrome/Profile 1", "edge/Default", "firefox/abc.default"
    ]


def test_meta_source_is_dict_not_json_string(browser_env, tmp_path):
    """meta 必须是 dict——服务端 BehaviorEvent.meta 声明为 dict，
    传 JSON 字符串会让整批事件 422。"""
    import browser_history as bh

    pusher = FakePusher()
    c = bh.BrowserHistoryCollector(pusher, cursor_file=str(tmp_path / "c.json"))
    c._default_cursor = 0
    c._cursors = {}
    c._collect_once()
    assert all(isinstance(e["meta"], dict) for e in pusher.events)


def test_per_source_cursor_prevents_starvation(browser_env, tmp_path):
    """按源独立游标：共用单游标时读完第一个源就会跳过其余源。"""
    import browser_history as bh

    cursor_file = tmp_path / "cursor.json"
    pusher = FakePusher()
    c = bh.BrowserHistoryCollector(pusher, cursor_file=str(cursor_file))
    c._default_cursor = 0
    c._cursors = {}
    c._collect_once()

    saved = json.loads(cursor_file.read_text())
    assert len(saved["sources"]) == 4, "每个源都要有自己的游标"

    # 第二轮增量应为空
    pusher.events.clear()
    c._collect_once()
    assert pusher.events == []


def test_legacy_single_cursor_migrated(browser_env, tmp_path):
    """旧格式 {"cursor": N} 要被尊重，不能触发历史重采。"""
    import browser_history as bh

    cursor_file = tmp_path / "cursor.json"
    cursor_file.write_text(json.dumps({"cursor": 13500000000000000}))

    pusher = FakePusher()
    c = bh.BrowserHistoryCollector(pusher, cursor_file=str(cursor_file))
    assert c._default_cursor == 13500000000000000
    c._collect_once()
    assert pusher.events == [], "旧游标之后没有新访问，不该重采"


# ── 时段推荐（use_count 的时段分桶）──────────────────────

@pytest.fixture
def launcher_store(tmp_path, monkeypatch):
    monkeypatch.setenv("LAUNCHER_STORE", str(tmp_path / "launcher.json"))
    launcher.save({
        "version": 1,
        "items": {
            "微信": {"alias": "微信", "app": "D:/W.exe", "use_count": 20,
                    "hours": {"9": 10, "10": 8}},
            "vscode": {"alias": "VSCode", "app": "D:/c.exe", "use_count": 15,
                       "hours": {"14": 12, "15": 5}},
            "b站": {"alias": "B站", "url": "https://b.com", "use_count": 30,
                   "hours": {"22": 25}},
            "百度": {"alias": "百度", "template": "https://b.com/s?wd={q}",
                    "use_count": 50},
        },
        "browsers": {},
    })


def test_suggestion_is_hour_aware(launcher_store):
    assert launcher.suggest_for_hour(9)[0]["alias"] == "微信"
    assert launcher.suggest_for_hour(14)[0]["alias"] == "VSCode"
    assert launcher.suggest_for_hour(22)[0]["alias"] == "B站"


def test_suggestion_excludes_search_only_items(launcher_store):
    """纯搜索模板不能"打开"，不该出现在推荐里（即便 use_count 最高）。"""
    for hour in range(0, 24, 6):
        assert "百度" not in [it["alias"] for it in launcher.suggest_for_hour(hour)]


def test_bump_records_hour_bucket(launcher_store):
    import datetime

    launcher.bump("微信")
    item = launcher.load()["items"]["微信"]
    assert item["use_count"] == 21
    hour = str(datetime.datetime.now(datetime.timezone.utc).astimezone().hour)
    assert item["hours"].get(hour, 0) >= 1


def test_suggestion_empty_without_history(tmp_path, monkeypatch):
    monkeypatch.setenv("LAUNCHER_STORE", str(tmp_path / "empty.json"))
    launcher.save({"version": 1, "items": {}, "browsers": {}})
    assert launcher.suggest_for_hour(9) == []
