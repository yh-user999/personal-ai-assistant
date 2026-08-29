"""第 6.26 课测试：消息全文搜索——命令解析 + LIKE 检索 + 片段截取。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.models.database import connect, init_db  # noqa: E402
from app.services import message_search  # noqa: E402


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    init_db()
    conn = connect()
    rows = [
        ("user", "李羽家的田地是三四亩，市价八到十二两", "2026-08-20T02:00:00+00:00"),
        ("assistant", "已记住：李羽家 田地 三四亩", "2026-08-20T02:01:00+00:00"),
        ("user", "今天把 RAG 检索调优做完了", "2026-08-28T02:00:00+00:00"),
        ("user", "明天9点提醒我开会", "2026-08-29T01:00:00+00:00"),
    ]
    conn.executemany(
        "INSERT INTO memories (sender, content, ts) VALUES (?, ?, ?)", rows
    )
    conn.commit()
    conn.close()
    yield


# ── 命令解析 ───────────────────────────────────────────────

def test_parse_search_command():
    assert message_search.parse_search_command("搜索聊天记录：田地") == "田地"
    assert message_search.parse_search_command("聊天记录搜 田地") == "田地"
    assert message_search.parse_search_command("帮我搜一下历史消息：开会") == "开会"
    assert message_search.parse_search_command("搜索一下北京天气") is None  # 无标记 → 归 LLM
    assert message_search.parse_search_command("帮我找包含todo的文件") is None  # 归执行器
    assert message_search.parse_search_command("今天吃什么") is None


# ── 检索 ───────────────────────────────────────────────────

def test_search_single_term(db):
    payload = message_search.search_messages("田地")
    assert payload["total"] == 2
    assert payload["results"][0]["sender_name"] == "小月"  # 时间倒序，assistant 在后
    assert "三四亩" in payload["results"][0]["snippet"]


def test_search_multi_term_and(db):
    payload = message_search.search_messages("田地 八到十二两")
    assert payload["total"] == 1
    assert payload["results"][0]["sender"] == "user"


def test_search_no_hit(db):
    payload = message_search.search_messages("不存在的词xyz")
    assert payload["total"] == 0
    assert payload["results"] == []


def test_search_empty_keyword(db):
    payload = message_search.search_messages("   ")
    assert payload["total"] == 0


def test_snippet_around_match():
    content = "今天" + "x" * 200 + "关键词" + "y" * 200 + "结束"
    terms = message_search._split_terms("关键词")
    snip = message_search._snippet(content, terms)
    assert "关键词" in snip
    assert snip.startswith("…") and snip.endswith("…")
    assert len(snip) < 200


def test_format_results(db):
    text = message_search.format_results("田地")
    assert "找到 2 条命中" in text
    assert "[08-20" in text  # UTC 02:00 → 北京时间 10:00
