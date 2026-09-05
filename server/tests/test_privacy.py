"""隐私过滤器单元测试（纯正则，无外部依赖）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "collector"))

from privacy_filter import sanitize, sanitize_event


def test_password_masked():
    assert "password=[REDACTED]" in sanitize("login password=abc123 ok")


def test_api_key_masked():
    assert "[API_KEY]" in sanitize("key sk-abcdefgh12345678 used")


def test_email_masked():
    # 两端脱敏规则合并到 common/redact 后统一为**部分打码**（保留可辨识度，
    # 与 README 记载的 "wfy***@163.com" 口径一致），不再整体替换为 [EMAIL]。
    out = sanitize("联系 test@example.com 处理")
    assert "test@example.com" not in out
    assert "tes***@example.com" in out


def test_phone_masked():
    # 同上：统一为 138****5678 的部分打码（README 口径），不再是 [PHONE]
    out = sanitize("手机号 13812345678 验证")
    assert "13812345678" not in out
    assert "138****5678" in out


def test_phone_masked_adjacent_to_chinese():
    """中文紧邻数字时也要脱敏。

    旧正则用 \\b 做边界，而中文在 Python re 里属于 \\w——"手机13812345678"
    的中文与数字之间不构成词边界，整串漏脱敏后原文上传服务器。
    """
    out = sanitize("手机13812345678")
    assert "13812345678" not in out
    assert sanitize("联系13812345678吧") == "联系138****5678吧"


def test_chinese_key_password_masked():
    """中文键名 + 全角冒号：窗口标题里最常见的形态，旧规则完全漏过。"""
    assert "[REDACTED]" in sanitize("密码：hunter2")
    assert "[REDACTED]" in sanitize("口令=abc123")
    assert "hunter2" not in sanitize("密码：hunter2")


def test_modern_token_formats_masked():
    """GitHub / AWS / JWT 三类主流凭证格式（旧规则只认 sk- 前缀）。"""
    assert sanitize("ghp_AbCdEfGhIjKlMnOpQrStUvWxYz012345") == "[GITHUB_TOKEN]"
    assert sanitize("AKIAIOSFODNN7EXAMPLE") == "[AWS_KEY]"
    assert "[JWT]" in sanitize("Bearer eyJhbGciOiJIUzI1NiJ9.eyJhIjoxfQ.sigval")
    assert "[PRIVATE_KEY]" in sanitize("-----BEGIN RSA PRIVATE KEY-----")


def test_version_string_not_masked_as_ip():
    """版本号与 IP 形态相同，靠前导词区分——旧规则把 1.2.3.4 打成 1.2.*.*。"""
    assert sanitize("版本 1.2.3.4 发布") == "版本 1.2.3.4 发布"
    assert sanitize("升级到 2.3.4.5") == "升级到 2.3.4.5"
    assert sanitize("v1.2.3.4") == "v1.2.3.4"


def test_long_digit_string_not_phone():
    """18 位订单号不该被当成手机号（旧规则的 \\b 会命中其中 11 位子串）。"""
    assert sanitize("订单号 13912345678901234") == "订单号 13912345678901234"


def test_event_detail_sanitized_and_truncated():
    ev = {"kind": "browser", "detail": "token: secret-abc123 " + "x" * 300}
    sanitize_event(ev)
    # 分隔符按原文保留（中文场景下 "密码：[REDACTED]" 比 "密码=[REDACTED]" 自然）
    assert "token:[REDACTED]" in ev["detail"]
    assert "secret-abc123" not in ev["detail"]
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

