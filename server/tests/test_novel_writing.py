"""第 6.25 课测试：小说写作增强——设定冲突检查 + 续写辅助 + 写作台账。"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.models.database import connect, init_db
from app.services import novel_writing

TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    init_db()
    yield


# ── ① 设定冲突检查 ─────────────────────────────────────────

def test_parse_conflict_command():
    assert novel_writing.parse_conflict_command("检查设定冲突：李羽一拳打死了仇家") == "李羽一拳打死了仇家"
    assert novel_writing.parse_conflict_command("设定冲突检查：李羽用了命丛") == "李羽用了命丛"
    assert novel_writing.parse_conflict_command("帮我查一下设定冲突：李羽会飞") == "李羽会飞"
    assert novel_writing.parse_conflict_command("今天天气不错") is None


def test_looks_like_file_path():
    assert novel_writing._looks_like_file_path("F:/wfy/第10章.txt")
    assert novel_writing._looks_like_file_path("C:\\Users\\x\\稿子.md")
    assert not novel_writing._looks_like_file_path("李羽走到田边看了看")


def test_parse_conflicts_json_tolerant():
    out = novel_writing.parse_conflicts_json('前面有噪声 [{"problem":"时间线冲突","quote":"他昨天杀了人"}]\n后面也有')
    assert len(out) == 1
    assert out[0]["problem"] == "时间线冲突"
    assert novel_writing.parse_conflicts_json("没有数组") == []
    assert novel_writing.parse_conflicts_json("[1, 2, 3]") == []  # 非 dict 条目跳过


def test_check_conflicts_no_conflict(db, monkeypatch):
    async def fake_chat(messages, **kw):
        return '[]'

    monkeypatch.setattr("app.core.llm.chat", fake_chat)

    import asyncio

    result = asyncio.run(novel_writing.check_conflicts("李羽在地里干活"))
    assert "未发现" in result["reply"]


def test_check_conflicts_found(db, monkeypatch):
    async def fake_chat(messages, **kw):
        return '[{"quote":"他昨天死了","problem":"李羽还活着","setting":"李羽是主角","basis":"设定卡","suggestion":"改成别人"}]'

    monkeypatch.setattr("app.core.llm.chat", fake_chat)

    import asyncio

    result = asyncio.run(novel_writing.check_conflicts("他昨天死了"))
    assert "发现 1 处" in result["reply"]
    assert "他昨天死了" in result["reply"]
    assert "设定卡" in result["reply"]


def test_check_conflicts_llm_failure(db, monkeypatch):
    async def fake_chat(messages, **kw):
        raise TimeoutError("boom")

    monkeypatch.setattr("app.core.llm.chat", fake_chat)

    import asyncio

    result = asyncio.run(novel_writing.check_conflicts("测试"))
    assert "稍后再试" in result["reply"]


# ── ② 续写辅助 ─────────────────────────────────────────────

def test_parse_continue_command():
    assert novel_writing.parse_continue_command("帮我续写：李羽推开门") == "李羽推开门"
    assert novel_writing.parse_continue_command("续写一下：他握紧了刀") == "他握紧了刀"
    assert novel_writing.parse_continue_command("帮我续写") is None  # 没给内容
    assert novel_writing.parse_continue_command("今天吃什么") is None


def test_continue_story(db, monkeypatch):
    async def fake_chat(messages, **kw):
        assert any("寂静杀戮" in m["content"] for m in messages)
        assert any("Sepia 小说生成规则" in m["content"] for m in messages)
        assert any("权威设定" in m["content"] for m in messages)
        return "李羽握紧了刀，向门外走去。"

    async def fake_search(query, **kw):
        return []

    monkeypatch.setattr("app.core.llm.chat", fake_chat)
    monkeypatch.setattr("app.core.knowledge.search_knowledge", fake_search)

    import asyncio

    out = asyncio.run(novel_writing.continue_story("李羽站在门前"))
    assert "握紧了刀" in out


# ── ③ 写作台账 ─────────────────────────────────────────────

def test_parse_writing_log():
    assert novel_writing.parse_writing_log("写作记录：第10章 3200字") == ("10", 3200)
    assert novel_writing.parse_writing_log("写作记录：3200字") == (None, 3200)
    assert novel_writing.parse_writing_log("写作记录：第10章写了3200字") == ("10", 3200)
    assert novel_writing.parse_writing_log("写作记录：abc字") is None
    assert novel_writing.parse_writing_log("写作记录：0字") is None
    assert novel_writing.parse_writing_log("随便聊聊") is None


def test_summary_empty(db):
    assert "还没有写作记录" in novel_writing.writing_summary()


def test_summary_stats_and_streak(db, monkeypatch):
    novel_writing.add_writing_log("9", 3000)
    novel_writing.add_writing_log("10", 3200)

    # 把一条记录改到 3 天前，验证近7天统计与连续天数只算今天
    conn = connect()
    past = (datetime.now(TZ) - timedelta(days=3)).astimezone(ZoneInfo("UTC")).isoformat()
    conn.execute("UPDATE writing_log SET created_at=? WHERE chapter='9'", (past,))
    conn.commit()
    conn.close()

    s = novel_writing.writing_summary()
    assert "累计：6,200 字" in s
    assert "今日：3,200 字" in s
    assert "连续写作：1 天" in s
    assert "第10章" in s
