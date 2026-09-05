"""快捷启动器测试（第 14 课）：注册语法 / 别名解析 / 搜索模板 / 指定浏览器 /
失败建议 / 采集器远程联动。

启动器存储经 LAUNCHER_STORE 指向临时文件；os.startfile 与 subprocess.Popen
全部打桩，测试不会真的打开任何东西。
"""
import json
import os

import pytest
from common import launcher
from desktop import local_exec


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("LAUNCHER_STORE", str(tmp_path / "launcher.json"))
    yield


@pytest.fixture
def fake_shell(monkeypatch):
    """打桩 startfile/Popen；state["fail"]=True 时 startfile 抛错（模拟打开失败）。"""
    calls = []
    state = {"fail": False}

    def fake_startfile(target):
        calls.append(("startfile", target))
        if state["fail"]:
            raise OSError(f"系统找不到 {target}")

    def fake_popen(cmd, **kwargs):
        calls.append(("popen", tuple(cmd)))

    # raising=False：os.startfile 仅 Windows 存在，Linux 测试环境按"新增属性"打桩
    monkeypatch.setattr(os, "startfile", fake_startfile, raising=False)
    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    return calls, state


# ── 预置与存储 ────────────────────────────────────────────

def test_first_load_seeds_presets():
    data = launcher.load()
    assert "b站" in data["items"]
    assert data["items"]["b站"]["url"] == "https://www.bilibili.com"
    assert "{q}" in data["items"]["b站"]["template"]
    assert isinstance(data["browsers"], dict)


# ── 注册语法解析 ──────────────────────────────────────────

def test_parse_add_variants():
    assert local_exec._parse_launcher("记住 打开B站 = https://www.bilibili.com") == (
        "add", "B站", {"url": "https://www.bilibili.com", "browser": ""},
    )
    assert local_exec._parse_launcher("记住 用chrome打开B站 = https://b23.tv") == (
        "add", "B站", {"url": "https://b23.tv", "browser": "chrome"},
    )
    assert local_exec._parse_launcher("记住 在B站搜索 = https://search.bilibili.com/all?keyword={q}") == (
        "add", "B站", {"template": "https://search.bilibili.com/all?keyword={q}", "browser": ""},
    )
    assert local_exec._parse_launcher("记住 打开微信 = D:/Program/WeChat/WeChat.exe") == (
        "add", "微信", {"app": "D:/Program/WeChat/WeChat.exe", "browser": ""},
    )
    assert local_exec._parse_launcher("记住 显示设置 = ms-settings:display") == (
        "add", "显示设置", {"shell": "ms-settings:display"},
    )
    assert local_exec._parse_launcher("忘掉B站") == ("remove", "B站")
    assert local_exec._parse_launcher("我的常用") == ("list",)
    assert local_exec._parse_launcher("帮我删掉快捷方式 微信") == ("remove", "微信")
    # 普通聊天不误触
    assert local_exec._parse_launcher("今天天气真不错") is None


def test_add_rejects_bad_template_and_scheme():
    ok, text = launcher.add_item("百度", template="https://www.baidu.com/s?wd=")
    assert not ok and "{q}" in text
    ok, text = launcher.add_item("坏协议", shell="foo://bar")
    assert not ok and "仅支持" in text


def test_add_plain_domain_gets_https():
    ok, _ = launcher.add_item("知乎", url="zhihu.com")
    assert ok
    assert launcher.find_item("知乎")["url"] == "https://zhihu.com"


def test_add_exe_looking_name_is_app_not_url():
    """裸 exe 名不能被当成网址。"""
    ok, _ = launcher.add_item("工具", app="wechat.exe")
    assert ok
    assert launcher.find_item("工具")["app"] == "wechat.exe"


# ── 打开流程 ──────────────────────────────────────────────

def test_open_registered_url(fake_shell):
    calls, _ = fake_shell
    handled, text = local_exec.try_execute("打开B站")
    assert handled and "✅" in text
    assert ("startfile", "https://www.bilibili.com") in calls


def test_open_registered_app(fake_shell):
    calls, _ = fake_shell
    local_exec.try_execute("记住 打开记事本 = C:/Windows/notepad.exe")
    handled, text = local_exec.try_execute("打开记事本")
    assert handled and "✅" in text
    assert ("startfile", "C:/Windows/notepad.exe") in calls


def test_open_use_count_ranks_first(fake_shell):
    """两个模糊候选时，用得多者优先。"""
    launcher.add_item("网易云音乐", url="https://music.163.com")
    launcher.add_item("网易邮箱", url="https://mail.163.com")
    launcher.bump("网易邮箱")
    launcher.bump("网易邮箱")
    handled, _ = local_exec.try_execute("打开网易")
    assert handled
    # "网易"同时是两者的前缀 → use_count 高的网易邮箱胜出
    assert ("startfile", "https://mail.163.com") in fake_shell[0]


def test_open_miss_then_raw_fail_gives_suggestion(fake_shell):
    _calls, state = fake_shell
    launcher.add_item("网易云音乐", url="https://music.163.com")
    state["fail"] = True
    handled, text = local_exec.try_execute("打开网抑云")
    assert handled and "❌" in text
    assert "网易云音乐" in text  # 相近项建议


def test_blocklist_still_enforced_for_unregistered(fake_shell):
    handled, text = local_exec.try_execute("打开evil.bat")
    assert handled and "❌" in text and "不允许打开" in text
    assert fake_shell[0] == []  # startfile 未被触达


# ── 搜索模板 / 指定浏览器 ─────────────────────────────────

def test_search_template(fake_shell):
    calls, _ = fake_shell
    handled, text = local_exec.try_execute("在b站搜索 ZCode 玩法")
    assert handled and "🔍" in text
    sf = [c for c in calls if c[0] == "startfile"]
    assert sf and sf[0][1].startswith("https://search.bilibili.com/all?keyword=")
    assert "ZCode" in sf[0][1]


def test_search_unknown_alias_falls_through():
    """没有注册模板的"在X搜索"不拦截，交给后续流程/LLM。"""
    handled, _ = local_exec.try_execute("在豆瓣搜索 电影")
    assert handled is False


def test_open_with_specific_browser(fake_shell, tmp_path):
    calls, _ = fake_shell
    browser_exe = tmp_path / "browser.exe"
    browser_exe.write_bytes(b"fake")
    data = launcher.load()
    data["browsers"]["mybrowser"] = str(browser_exe).replace("\\", "/")
    launcher.save(data)
    handled, text = local_exec.try_execute("用mybrowser打开B站")
    assert handled and "✅" in text
    assert ("popen", (str(browser_exe).replace("\\", "/"), "https://www.bilibili.com")) in calls


def test_browser_registration_and_use(fake_shell, tmp_path):
    calls, _ = fake_shell
    browser_exe = tmp_path / "msedge.exe"
    browser_exe.write_bytes(b"fake")
    data = launcher.load()
    data["browsers"]["edge"] = str(browser_exe).replace("\\", "/")
    launcher.save(data)
    handled, text = local_exec.try_execute("记住 用edge打开知乎 = zhihu.com")
    assert handled and "已记住" in text
    handled, text = local_exec.try_execute("用edge打开知乎")
    assert ("popen", (str(browser_exe).replace("\\", "/"), "https://zhihu.com")) in calls


# ── 管理：列表 / 遗忘 ─────────────────────────────────────

def test_list_and_forget(fake_shell):
    handled, text = local_exec.try_execute("我的常用")
    assert handled and "B站" in text
    handled, text = local_exec.try_execute("忘掉B站")
    assert handled and "已忘掉" in text
    assert launcher.find_item("B站") is None
    handled, text = local_exec.try_execute("忘掉不存在的东西xyz")
    assert handled and "没有找到" in text


# ── 采集器（远程队列）联动 ────────────────────────────────

def test_collector_executor_resolves_alias(fake_shell):
    from collector.executor import Executor

    ok, text = Executor("http://x", "")._execute("open", "B站")
    assert ok and "已打开" in text
    assert ("startfile", "https://www.bilibili.com") in fake_shell[0]


def test_collector_executor_raw_still_blocked(fake_shell):
    from collector.executor import Executor

    ok, text = Executor("http://x", "")._execute("open", "evil.bat")
    assert not ok
    assert "不允许打开" in text
    assert fake_shell[0] == []  # startfile 未被触达


# ── 存储格式 ──────────────────────────────────────────────

def test_store_is_valid_json_with_casefold_keys(tmp_path):
    launcher.add_item("GitHub", url="https://github.com")
    data = json.loads(launcher.store_path().read_text(encoding="utf-8"))
    assert "github" in data["items"]  # 键为 casefold 别名，大小写不敏感命中
    assert data["items"]["github"]["alias"] == "GitHub"
