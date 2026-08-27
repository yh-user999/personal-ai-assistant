"""第 6.14 课：服务器入库前统一脱敏（手机号/邮箱/公网IP/身份证/自定义词）。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")
os.environ.setdefault("DB_PATH", "/tmp/test_sanitize.db")

from app.services.sanitize import sanitize  # noqa: E402


def test_phone_masked():
    assert sanitize("联系电话：13135582351") == "联系电话：131****2351"


def test_email_masked():
    assert sanitize("邮箱 wfy3366@163.com 收件") == "邮箱 wfy***@163.com 收件"


def test_public_ip_masked():
    assert sanitize("访问 101.33.229.73:8082") == "访问 101.33.*.*:8082"
    assert sanitize("117.72.11.59") == "117.72.*.*"


def test_private_ip_kept():
    assert sanitize("curl 127.0.0.1:8000") == "curl 127.0.0.1:8000"
    assert sanitize("192.168.1.100 10.0.0.5") == "192.168.1.100 10.0.0.5"
    assert sanitize("172.16.0.1") == "172.16.0.1"


def test_idcard_masked():
    assert sanitize("身份证 110101199003078888") == "身份证 110101********8888"


def test_custom_terms(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "sensitive_terms", "韦飞宇;桂林电子科技大学")
    text = sanitize("韦飞宇毕业于桂林电子科技大学")
    assert text == "已脱敏毕业于已脱敏"
    monkeypatch.setattr(settings, "sensitive_terms", "")
    assert sanitize("普通内容") == "普通内容"


def test_empty_untouched():
    assert sanitize("") == ""
    assert sanitize("无敏感信息的一句话") == "无敏感信息的一句话"


def test_masked_output_is_stable():
    """脱敏结果本身不再命中任何规则（幂等）。"""
    once = sanitize("电话13135582351 邮箱wfy3366@163.com 服务器101.33.229.73")
    twice = sanitize(once)
    assert once == twice
