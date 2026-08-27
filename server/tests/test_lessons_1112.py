"""第 11/12 课测试：目标命令解析 + unresolved 检测 + 执行器解析/白名单。"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")
os.environ.setdefault("DB_PATH", "/tmp/test_lessons_1112.db")

from app.models.database import connect, init_db  # noqa: E402
from app.services import executor, goals, unresolved  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    init_db()
    conn = connect()
    for t in ("goals", "unresolved_issues", "executor_commands", "memories"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()
    yield


# ── Goal 系统 ─────────────────────────────────────────────

def test_goal_commands_parse():
    assert goals.parse_goal_command("目标：12周AI项目第5周完成RAG调优") == ("create", "12周AI项目第5周完成RAG调优")
    assert goals.parse_goal_command("目标完成：RAG调优") == ("done", "RAG调优")
    assert goals.parse_goal_command("目标进度：做到第4周") == ("progress", "做到第4周")
    assert goals.parse_goal_command("今天天气不错") is None


def test_goal_lifecycle():
    goals.add_goal("学习FastAPI")
    text = goals.get_goals_injection()
    assert "学习FastAPI" in text
    assert goals.update_progress("已学完路由")
    assert "已学完路由" in goals.get_goals_injection()
    assert goals.complete_goal("FastAPI")
    assert goals.get_goals_injection() == ""  # 完成后不再是活跃目标


# ── unresolved 追踪 ───────────────────────────────────────

def test_unresolved_detection():
    assert unresolved.detect_unresolved("这个问题还没解决，先放着")
    assert unresolved.detect_resolved("那个问题解决了")
    assert not unresolved.detect_unresolved("帮我写代码")


def test_unresolved_lifecycle():
    unresolved.add_issue("RAG向量维度报错")
    assert "RAG向量维度报错" in unresolved.get_open_issues_injection()
    assert unresolved.count_open() == 1
    assert unresolved.resolve_latest()
    assert unresolved.count_open() == 0


# ── 执行器 ────────────────────────────────────────────────

def test_executor_parse():
    assert executor.parse_executor_command("帮我打开VSCode") == ("open", "VSCode")
    assert executor.parse_executor_command("看看桌面目录里有什么") == ("list_dir", "桌面")
    assert executor.parse_executor_command("读一下 F:/notes.txt") == ("read_file", "F:/notes.txt")
    assert executor.parse_executor_command("今天吃什么") is None


def test_executor_whitelist(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "executor_roots", "F:/, C:/Users/wfy33")
    assert executor.check_roots("F:/Projects/a") is True
    assert executor.check_roots("C:/Users/wfy33/Desktop") is True
    assert executor.check_roots("D:/secret") is False
    monkeypatch.setattr(settings, "executor_roots", "")
    assert executor.check_roots("F:/anything") is False  # 未配置=全禁止


def test_executor_queue():
    cmd_id = executor.enqueue("open", "notepad")
    pending = executor.get_pending()
    assert pending["id"] == cmd_id
    assert pending["action"] == "open"
    executor.mark_result(cmd_id, True, "已打开 notepad")
    assert executor.get_pending() is None  # 已完成不再出现
