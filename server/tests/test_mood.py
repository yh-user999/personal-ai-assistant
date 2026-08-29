"""第 6.23 课测试：情绪感知（零成本规则检测）。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

from app.services.mood import detect_mood  # noqa: E402


def test_tired():
    g = detect_mood("今天累死了，先不聊了")
    assert "疲惫" in g
    assert "简短" in g


def test_urgent():
    g = detect_mood("赶紧帮我看看这个报错，在线等")
    assert "着急" in g
    assert "直接给答案" in g


def test_annoyed():
    g = detect_mood("烦死了，这个bug又出现了")
    assert "情绪不佳" in g
    assert "共情" in g


def test_low():
    g = detect_mood("有点难过，感觉没什么意思")
    assert "低落" in g


def test_neutral_empty():
    assert detect_mood("帮我看看F盘的目录") == ""
