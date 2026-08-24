"""隐私过滤器单元测试（纯正则，无外部依赖）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "collector"))

from privacy_filter import sanitize, sanitize_event  # noqa: E402


def test_password_masked():
    assert "password=[REDACTED]" in sanitize("login password=abc123 ok")


def test_api_key_masked():
    assert "[API_KEY]" in sanitize("key sk-abcdefgh12345678 used")


def test_email_masked():
    assert "[EMAIL]" in sanitize("联系 wfy3366@163.com 处理")


def test_phone_masked():
    assert "[PHONE]" in sanitize("手机号 13812345678 验证")


def test_event_detail_sanitized_and_truncated():
    ev = {"kind": "browser", "detail": "token: secret-abc123 " + "x" * 300}
    sanitize_event(ev)
    assert "token=[REDACTED]" in ev["detail"]
    assert len(ev["detail"]) <= 200


def test_plain_text_untouched():
    assert sanitize("正常的中文内容没有敏感信息") == "正常的中文内容没有敏感信息"
