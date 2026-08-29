"""第 6.24 课测试：本地文件搜索——命令解析 + 白名单内按名/内容查找。"""
import os
import sys
import tempfile

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from desktop import local_exec  # noqa: E402


@pytest.fixture
def sandbox(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("EXECUTOR_ALLOWED_ROOTS", td.replace("\\", "/"))
        # 目录结构：proj/todo.md、proj/deep/计划-todo.txt、proj/other.md（内容含关键词）
        proj = os.path.join(td, "proj")
        os.makedirs(os.path.join(proj, "deep"))
        open(os.path.join(proj, "todo.md"), "w").write("# 待办")
        open(os.path.join(proj, "deep", "计划-todo.txt"), "w").write("计划")
        open(os.path.join(proj, "other.md"), "w").write("这里提到了 周报模板 三个字")
        # >300KB 的大文件：内容搜索应跳过（护栏）
        open(os.path.join(proj, "big.md"), "w").write("周报模板" + "x" * (300 * 1024))
        yield td


# ── 解析 ───────────────────────────────────────────────────

def test_parse_find_all_roots():
    assert local_exec._parse("帮我找包含todo的文件") == ("find_files", "", "todo")
    assert local_exec._parse("找一下todo文件") == ("find_files", "", "todo")
    assert local_exec._parse("搜索名字里有计划的文档") == ("find_files", "", "计划")
    assert local_exec._parse("帮我找todo的文件") == ("find_files", "", "todo")


def test_parse_find_in_dir_a():
    assert local_exec._parse("找F:/projects里的todo文件") == ("find_files", "F:/projects", "todo")
    assert local_exec._parse("在F盘里找包含todo的文件") == ("find_files", "F:/", "todo")
    assert local_exec._parse("找一下F盘里的todo文件") == ("find_files", "F:/", "todo")


def test_parse_content_marker():
    assert local_exec._parse("搜索内容包含密码的文件") == ("find_files", "", "content:密码")
    assert local_exec._parse("在F盘里找内容包含todo的文件") == ("find_files", "F:/", "content:todo")


def test_parse_find_ignores_ambiguous():
    """无标记/无后缀的模糊搜索句式不算文件搜索 → 交给 LLM。"""
    assert local_exec._parse("搜索淘宝里的switch") is None
    assert local_exec._parse("帮我找todo") is None  # 没说是"文件"
    assert local_exec._parse("搜索一下北京天气") is None


def test_parse_not_find():
    assert local_exec._parse("帮我打开F:/a.txt") is not None  # open 不受影响
    assert local_exec._parse("今天吃什么") is None


# ── 执行 ───────────────────────────────────────────────────

def test_find_by_name_in_dir(sandbox):
    ok, text = local_exec._execute("find_files", os.path.join(sandbox, "proj"), "todo")
    assert ok
    assert "todo.md" in text and "计划-todo.txt" in text
    assert "other.md" not in text


def test_find_all_roots(sandbox):
    ok, text = local_exec._execute("find_files", "", "todo")
    assert ok
    assert "todo.md" in text


def test_find_content_mode(sandbox):
    ok, text = local_exec._execute("find_files", os.path.join(sandbox, "proj"), "content:周报模板")
    assert ok
    assert "other.md" in text


def test_find_content_skips_huge_file(sandbox):
    """>300KB 的文件不做内容扫描（护栏生效）。"""
    ok, text = local_exec._execute("find_files", os.path.join(sandbox, "proj"), "content:周报模板")
    assert ok
    assert "big.md" not in text


def test_find_no_hit_is_ok(sandbox):
    ok, text = local_exec._execute("find_files", os.path.join(sandbox, "proj"), "不存在的词xyz")
    assert ok
    assert "没有找到" in text


def test_find_blocked_outside_root(sandbox):
    outside = tempfile.mkdtemp()
    try:
        ok, text = local_exec._execute("find_files", outside, "todo")
        assert not ok
        assert "🔒" in text
    finally:
        os.rmdir(outside)


def test_try_execute_find_end_to_end(sandbox):
    handled, text = local_exec.try_execute(f"在{sandbox}里找包含todo的文件")
    assert handled
    assert "✅ [find_files]" in text
    assert "todo.md" in text


def test_try_execute_find_all_roots_no_whitelist_error(monkeypatch):
    monkeypatch.delenv("EXECUTOR_ALLOWED_ROOTS", raising=False)
    handled, text = local_exec.try_execute("帮我找包含todo的文件")
    assert handled
    assert "白名单" in text


# ── 第 6.24 课扩展：采集器远程搜索通道（search_files 入队动作）──

def test_collector_search_files(sandbox):
    """Windows 采集器执行器：JSON [目录, 关键词] → 与桌面端同一公共实现。"""
    import json

    from collector.executor import Executor

    ex = Executor("http://x", "")
    ok, text = ex._execute("search_files", json.dumps([os.path.join(sandbox, "proj"), "todo"]))
    assert ok
    assert "todo.md" in text and "计划-todo.txt" in text


def test_collector_search_all_roots(sandbox):
    import json

    from collector.executor import Executor

    ex = Executor("http://x", "")
    ok, text = ex._execute("search_files", json.dumps(["", "todo"]))
    assert ok
    assert "todo.md" in text


def test_collector_search_blocked(sandbox):
    import json

    from collector.executor import Executor

    ex = Executor("http://x", "")
    outside = tempfile.mkdtemp()
    try:
        ok, text = ex._execute("search_files", json.dumps([outside, "todo"]))
        assert not ok
        assert "🔒" in text
    finally:
        os.rmdir(outside)


def test_server_parse_search_files():
    """服务端聊天解析：目录句式 + 全根句式 + 误吞防护（与桌面同规则）。"""
    import json as _json
    from app.services import executor as srv_exec

    assert srv_exec.parse_executor_command("帮我找包含todo的文件") == (
        "search_files",
        _json.dumps(["", "todo"], ensure_ascii=False),
    )
    act, target = srv_exec.parse_executor_command("在F盘里找内容包含todo的文件")
    assert act == "search_files"
    assert _json.loads(target) == ["F:/", "content:todo"]
    assert srv_exec.parse_executor_command("搜索一下北京天气") is None
    assert srv_exec.parse_executor_command("搜索淘宝里的switch") is None
    # 目录为空时 unpack 返回空列表（全白名单搜索豁免目录校验）
    assert srv_exec.unpack_paths("search_files", _json.dumps(["", "todo"])) == []
