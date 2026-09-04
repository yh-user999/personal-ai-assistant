"""第 11/12 课测试：目标命令解析 + unresolved 检测 + 执行器解析/白名单。"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

from app.config import settings  # noqa: E402
from app.models.database import connect, init_db, reset_connections  # noqa: E402
from app.services import executor, goals, unresolved  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """独立临时库。原实现对 goals/unresolved_issues/executor_commands/memories
    四张表做 DELETE，而这些 DELETE 一直跑在生产库上（DB_PATH 环境变量隔离无效）。"""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    reset_connections()
    init_db()
    yield
    reset_connections()


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


def test_executor_parse_file_ops():
    """第 13 课：文件手解析（双路径 JSON 打包）+ 脚本脚远程禁用。"""
    action, target = executor.parse_executor_command("帮我复制F:/a.txt到F:/b.txt")
    assert action == "copy"
    assert executor.unpack_paths(action, target) == ["F:/a.txt", "F:/b.txt"]
    action, target = executor.parse_executor_command("把F:/a移动到F:/b")
    assert action == "move"
    assert executor.unpack_paths(action, target) == ["F:/a", "F:/b"]
    assert executor.parse_executor_command("帮我运行F:/x.py") is None  # 脚本不允许远程执行


def test_executor_whitelist_dual(monkeypatch):
    """双路径操作：任一路径出白名单即拒绝。"""
    from app.config import settings

    monkeypatch.setattr(settings, "executor_allowed_roots", "F:/")
    action, target = executor.parse_executor_command("帮我复制F:/a.txt到D:/b.txt")
    paths = executor.unpack_paths(action, target)
    assert [executor.check_roots(p) for p in paths] == [True, False]


def test_executor_drive_normalize():
    """口语盘符规范化：F盘→F:/，能过白名单。"""
    assert executor.normalize_target("F盘") == "F:/"
    assert executor.normalize_target("c盘/项目") == "C:/项目"
    assert executor.normalize_target("F盘的目录x") == "F:/目录x"
    action, target = executor.parse_executor_command("看看F盘的目录里有什么")
    assert action == "list_dir"
    assert target.startswith("F:/")


def test_executor_whitelist(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "executor_allowed_roots", "F:/, C:/Users/wfy33")
    assert executor.check_roots("F:/Projects/a") is True
    assert executor.check_roots("C:/Users/wfy33/Desktop") is True
    assert executor.check_roots("D:/secret") is False
    monkeypatch.setattr(settings, "executor_allowed_roots", "")
    assert executor.check_roots("F:/anything") is False  # 未配置=全禁止


def test_executor_queue():
    cmd_id = executor.enqueue("open", "notepad")
    pending = executor.get_pending()
    assert pending["id"] == cmd_id
    assert pending["action"] == "open"
    executor.mark_result(cmd_id, True, "已打开 notepad")
    assert executor.get_pending() is None  # 已完成不再出现


def test_executor_stale_expiry():
    """僵尸指令防护：pending 超过 30 分钟自动标记失败，不再返回执行。"""
    conn = connect()
    conn.execute(
        "INSERT INTO executor_commands (action, target, status, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("list_dir", "F:/", "pending", "2020-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    assert executor.get_pending() is None  # 过期指令不派发
    conn = connect()
    row = conn.execute(
        "SELECT status, result FROM executor_commands WHERE action='list_dir'"
    ).fetchone()
    conn.close()
    assert row["status"] == "failed"
    assert "过期" in row["result"]


def test_executor_fresh_pending_ok():
    """刚入队的指令不受过期逻辑影响。"""
    cmd_id = executor.enqueue("open", "notepad")
    assert executor.get_pending()["id"] == cmd_id


def test_lessons_rebuild_preserves_hit_statistics():
    """lessons 去重重建时保留统计列，避免注入命中数据归零。"""
    import sqlite3
    from app.models import database

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            context TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'style',
            hit_count INTEGER DEFAULT 0,
            last_hit_at TEXT
        );
        INSERT INTO lessons(content, context, created_at, kind, hit_count, last_hit_at)
        VALUES ('纠正内容', 'ctx', '2026-01-01', 'fact', 7, '2026-01-02');
        INSERT INTO lessons(content, context, created_at, kind, hit_count, last_hit_at)
        VALUES ('纠正内容', 'newer', '2026-01-03', 'fact', 99, '2026-01-04');
        """
    )
    database._migrate_lessons(conn)
    row = conn.execute(
        "SELECT content, hit_count, last_hit_at FROM lessons"
    ).fetchone()
    assert row["content"] == "纠正内容"
    assert row["hit_count"] == 7
    assert row["last_hit_at"] == "2026-01-02"
    conn.close()
