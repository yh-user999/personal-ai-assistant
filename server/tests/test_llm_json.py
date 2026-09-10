"""LLM JSON 抽取：模型输出常带围栏、前缀说明或思考段。"""
import pytest

from app.chat import llm_json


def test_plain_json():
    assert llm_json.extract_json_object('{"a": 1}') == {"a": 1}


def test_fenced_json():
    text = '```json\n{"mode": "reasoning"}\n```'
    assert llm_json.extract_json_object(text) == {"mode": "reasoning"}


def test_preamble_before_json():
    text = '好的，我来规划一下：\n{"mode": "creative"}\n以上。'
    assert llm_json.extract_json_object(text) == {"mode": "creative"}


def test_nested_object_kept_whole():
    text = 'think... {"a": {"b": 1}, "c": [1, 2]} done'
    assert llm_json.extract_json_object(text) == {"a": {"b": 1}, "c": [1, 2]}


def test_truncated_json_returns_none():
    assert llm_json.extract_json_object('{"mode": "reasoning", "constraints": ["一半') is None


@pytest.mark.parametrize("text", ["", "没有任何 JSON", "只有 { 没有闭合", "[1,2,3]"])
def test_non_object_or_garbage_returns_none(text):
    assert llm_json.extract_json_object(text) is None


def test_log_unparsed_does_not_raise(caplog):
    llm_json.log_unparsed("测试", "不是 JSON")
    assert any("无法解析" in record.message for record in caplog.records)
