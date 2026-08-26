"""多轮历史测试：正序返回 + 截断。"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")
os.environ.setdefault("DB_PATH", "/tmp/test_history.db")

from app.core import memory  # noqa: E402
from app.models.database import connect, init_db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    init_db()
    conn = connect()
    conn.execute("DELETE FROM memories")
    conn.commit()
    conn.close()
    yield


def _seed(sender, content):
    conn = connect()
    conn.execute(
        "INSERT INTO memories (sender, content, ts) VALUES (?, ?, '2026-08-26T00:00:00+00:00')",
        (sender, content),
    )
    conn.commit()
    conn.close()


def test_history_ordered_asc_and_roles():
    _seed("user", "我们的项目进行到第几课了？")
    _seed("assistant", "六课全部完成。")
    _seed("user", "你确定吗，再确认一下")
    hist = memory.get_recent_history(8)
    assert [h["role"] for h in hist] == ["user", "assistant", "user"]  # 正序
    assert hist[-1]["content"] == "你确定吗，再确认一下"  # 最新在最后


def test_history_limit_and_truncate():
    for i in range(20):
        _seed("user", f"消息{i}")
    hist = memory.get_recent_history(8)
    assert len(hist) == 8
    long_msg = "长" * 600
    _seed("assistant", long_msg)
    hist2 = memory.get_recent_history(3)
    assert len(hist2[-1]["content"]) <= 500  # 截断生效


def test_older_summaries_picked():
    """窗口外的对话摘要被提取（复用 consolidation 的 summary 字段）。"""
    conn = connect()
    # 窗口外（更早）的对话带摘要
    conn.execute(
        "INSERT INTO memories (sender, content, summary, ts) VALUES "
        "('user', '早话题A', '用户在讨论RAG调优', '2026-08-26T01:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO memories (sender, content, summary, ts) VALUES "
        "('assistant', '早回复', '__merged__', '2026-08-26T01:01:00+00:00')"
    )
    conn.commit()
    conn.close()
    for i in range(10):  # 窗口内 10 条无摘要
        _seed("user", f"窗口消息{i}")
    summaries = memory.get_older_summaries(window_size=8, limit=4)
    assert any("RAG调优" in s for s in summaries)  # __merged__ 被跳过，真摘要被取到
