"""窗口标题 → 应用名猜测的单元测试（纯函数，跨平台可测）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "collector"))

from window_monitor import guess_app_from_title  # noqa: E402


def test_chrome_suffix():
    assert guess_app_from_title("基于QQ机器人构建个人智能助手方案 — DSH Local Build - Google Chrome") == "Google Chrome"


def test_notepad_suffix():
    assert guess_app_from_title("*你好，今天天气 - Notepad") == "Notepad"


def test_bare_title():
    assert guess_app_from_title("任务管理器") == "任务管理器"


def test_empty_title():
    assert guess_app_from_title("") == "unknown"
