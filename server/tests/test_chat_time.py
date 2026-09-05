"""时间/日期快速问答测试（零成本规则路由，不烧 LLM）。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

from app.api.chat import parse_time_question


def test_time_questions_match():
    assert parse_time_question("几点了") is not None
    assert parse_time_question("现在几点") is not None
    assert parse_time_question("今天星期几") is not None
    assert parse_time_question("今天几号") is not None
    assert parse_time_question("现在时间") is not None


def test_time_reply_format():
    reply = parse_time_question("几点了")
    assert reply.startswith("现在是")
    assert "月" in reply and "日" in reply and "星期" in reply


def test_non_time_questions_pass():
    assert parse_time_question("帮我看看代码") is None
    assert parse_time_question("今天天气怎么样") is None
    assert parse_time_question("今天的目标是什么") is None
    assert parse_time_question("几点开始上课") is None  # 问课程时间，不是当前时间
