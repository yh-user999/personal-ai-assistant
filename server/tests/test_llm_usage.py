"""LLM token 用量记账测试：累加 / 清零 / 字段兼容 / 记账失败不影响回复。"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

from app.core import llm


class _Usage:
    def __init__(self, prompt=0, completion=0, cached=None, details=None):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        if cached is not None:
            self.prompt_cache_hit_tokens = cached
        if details is not None:
            self.prompt_tokens_details = details


class _Resp:
    def __init__(self, usage=None, content="ok"):
        self.usage = usage
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


@pytest.fixture(autouse=True)
def clean_usage():
    llm.reset_usage()
    yield
    llm.reset_usage()


def test_records_and_accumulates():
    llm._record_usage(_Resp(_Usage(prompt=1000, completion=100)))
    llm._record_usage(_Resp(_Usage(prompt=500, completion=50)))
    u = llm.get_usage()
    assert u["calls"] == 2
    assert u["prompt"] == 1500
    assert u["completion"] == 150


def test_deepseek_cache_field():
    """DeepSeek 原生字段 prompt_cache_hit_tokens。"""
    llm._record_usage(_Resp(_Usage(prompt=1000, completion=10, cached=800)))
    assert llm.get_usage()["cached"] == 800


def test_openai_style_cache_field_object():
    """OpenAI 系嵌套字段（对象形态）。"""
    details = type("D", (), {"cached_tokens": 640})()
    llm._record_usage(_Resp(_Usage(prompt=1000, completion=10, details=details)))
    assert llm.get_usage()["cached"] == 640


def test_openai_style_cache_field_dict():
    """中转常把 details 反序列化成 dict。"""
    llm._record_usage(_Resp(_Usage(prompt=1000, completion=10, details={"cached_tokens": 320})))
    assert llm.get_usage()["cached"] == 320


def test_empty_details_no_crash():
    """实测某中转返回 prompt_tokens_details={}——不能因此炸掉。"""
    llm._record_usage(_Resp(_Usage(prompt=100, completion=10, details={})))
    u = llm.get_usage()
    assert u["cached"] == 0 and u["prompt"] == 100


def test_missing_usage_is_ignored():
    llm._record_usage(_Resp(usage=None))
    assert llm.get_usage()["calls"] == 0


def test_reset_returns_snapshot_and_zeroes():
    llm._record_usage(_Resp(_Usage(prompt=200, completion=20)))
    snap = llm.reset_usage()
    assert snap["prompt"] == 200
    assert llm.get_usage() == {"calls": 0, "prompt": 0, "completion": 0, "cached": 0}


def test_get_usage_returns_copy():
    """外部拿到的是副本，改它不该污染内部计数。"""
    llm._record_usage(_Resp(_Usage(prompt=10, completion=1)))
    snap = llm.get_usage()
    snap["prompt"] = 999999
    assert llm.get_usage()["prompt"] == 10
