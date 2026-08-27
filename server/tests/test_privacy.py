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
    assert "[EMAIL]" in sanitize("联系 test@example.com 处理")


def test_phone_masked():
    assert "[PHONE]" in sanitize("手机号 13812345678 验证")


def test_event_detail_sanitized_and_truncated():
    ev = {"kind": "browser", "detail": "token: secret-abc123 " + "x" * 300}
    sanitize_event(ev)
    assert "token=[REDACTED]" in ev["detail"]
    assert len(ev["detail"]) <= 200


def test_plain_text_untouched():
    assert sanitize("正常的中文内容没有敏感信息") == "正常的中文内容没有敏感信息"


def test_public_ip_masked_in_name():
    assert sanitize("8.8.8.8:8082") == "8.8.*.*:8082"
    assert sanitize("1.2.3.4:8000") == "1.2.*.*:8000"


def test_private_ip_kept():
    assert sanitize("127.0.0.1:8000") == "127.0.0.1:8000"
    assert sanitize("192.168.1.100") == "192.168.1.100"
    assert sanitize("10.0.0.5") == "10.0.0.5"


def test_event_name_sanitized():
    ev = {"kind": "browser", "name": "8.8.8.8:8082", "detail": "面板"}
    sanitize_event(ev)
    assert ev["name"] == "8.8.*.*:8082"
    assert ev["detail"] == "面板"

