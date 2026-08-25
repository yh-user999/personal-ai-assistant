"""工作日志时间解析单元测试：中文口语时间范围。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.worklog import _parse_time_range  # noqa: E402


def test_cn_afternoon_range():
    assert _parse_time_range("下午3点到5点完成了记忆系统实验") == "15:00-17:00"


def test_cn_morning_range():
    assert _parse_time_range("上午9点到11点开会") == "09:00-11:00"


def test_cn_evening_range():
    assert _parse_time_range("晚上7点到9点写代码") == "19:00-21:00"


def test_cn_no_dian_between():
    assert _parse_time_range("下午2-5点调RAG性能") == "14:00-17:00"


def test_digit_format_untouched():
    assert _parse_time_range("14:00至16:30调试") == "14:00-16:30"


def test_no_time_range():
    assert _parse_time_range("随便写点东西") == ""
