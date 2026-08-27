"""第 13 课测试：文件手（复制/备份/移动/重命名）+ 脚本脚（run_script，仅本地）。

覆盖：解析（含口语变体）、执行、白名单拒绝、超时终止、采集器 JSON 双路径。
"""
import json
import os
import sys
import tempfile

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from desktop import local_exec  # noqa: E402


@pytest.fixture
def sandbox(monkeypatch):
    """白名单 = 临时目录本身。"""
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("EXECUTOR_ALLOWED_ROOTS", td.replace("\\", "/"))
        yield td


# ── 解析 ───────────────────────────────────────────────────

def test_parse_file_ops():
    assert local_exec._parse("帮我复制F:/a.txt到F:/b.txt") == ("copy", "F:/a.txt", "F:/b.txt")
    assert local_exec._parse("备份 F:/proj 到 F:/backup") == ("backup", "F:/proj", "F:/backup")
    assert local_exec._parse("移动F:/a至F:/b") == ("move", "F:/a", "F:/b")
    assert local_exec._parse("把F:/a移动到F:/b") == ("move", "F:/a", "F:/b")
    assert local_exec._parse("把F:/a.txt改名为b.txt") == ("rename", "F:/a.txt", "b.txt")
    assert local_exec._parse("重命名F:/a.txt为b.txt") == ("rename", "F:/a.txt", "b.txt")


def test_parse_run_script():
    assert local_exec._parse("帮我运行F:/scripts/backup.py") == ("run_script", "F:/scripts/backup.py", "")
    assert local_exec._parse("跑一下 F:/s/x.bat") == ("run_script", "F:/s/x.bat", "")
    assert local_exec._parse("帮我跑脚本") is None  # 没给脚本路径 → 交给 LLM


# ── 文件手执行 ─────────────────────────────────────────────

def test_copy_file_into_dir(sandbox):
    src = os.path.join(sandbox, "a.txt")
    open(src, "w").write("hello" * 1000)
    dst_dir = os.path.join(sandbox, "out")
    os.makedirs(dst_dir)
    ok, text = local_exec._execute("copy", src, dst_dir)
    assert ok
    assert os.path.isfile(os.path.join(dst_dir, "a.txt"))
    assert "已复制" in text and "KB" in text


def test_backup_creates_timestamp_dir(sandbox):
    src = os.path.join(sandbox, "a.txt")
    open(src, "w").write("x")
    dst = os.path.join(sandbox, "bk")
    os.makedirs(dst)
    ok, _ = local_exec._execute("backup", src, dst)
    assert ok
    subs = [d for d in os.listdir(dst) if d.startswith("backup-")]
    assert len(subs) == 1
    assert os.path.isfile(os.path.join(dst, subs[0], "a.txt"))


def test_copy_dir_merge(sandbox):
    src = os.path.join(sandbox, "d1")
    os.makedirs(src)
    open(os.path.join(src, "f.txt"), "w").write("1")
    ok, _ = local_exec._execute("copy", src, os.path.join(sandbox, "d2"))
    assert ok
    assert os.path.isfile(os.path.join(sandbox, "d2", "f.txt"))


def test_move_and_rename(sandbox):
    src = os.path.join(sandbox, "m.txt")
    open(src, "w").write("m")
    dst_dir = os.path.join(sandbox, "mv")
    os.makedirs(dst_dir)
    ok, _ = local_exec._execute("move", src, dst_dir)
    assert ok
    assert not os.path.exists(src)
    assert os.path.isfile(os.path.join(dst_dir, "m.txt"))
    ok, _ = local_exec._execute("rename", os.path.join(dst_dir, "m.txt"), "n.txt")
    assert ok
    assert os.path.isfile(os.path.join(dst_dir, "n.txt"))


def test_copy_missing_src(sandbox):
    ok, text = local_exec._execute("copy", os.path.join(sandbox, "nope.txt"), os.path.join(sandbox, "x"))
    assert not ok
    assert "源不存在" in text


# ── 脚本脚执行 ─────────────────────────────────────────────

def test_run_script_ok(sandbox):
    script = os.path.join(sandbox, "hello.py")
    open(script, "w", encoding="utf-8").write("print('hello 小月')")
    ok, text = local_exec._execute("run_script", script)
    assert ok
    assert "hello 小月" in text and "exit 0" in text


def test_run_script_bad_ext(sandbox):
    script = os.path.join(sandbox, "x.ps1")
    open(script, "w").write("echo hi")
    ok, text = local_exec._execute("run_script", script)
    assert not ok
    assert "仅支持" in text


def test_run_script_timeout(sandbox, monkeypatch):
    monkeypatch.setattr(local_exec, "SCRIPT_TIMEOUT", 1)
    script = os.path.join(sandbox, "slow.py")
    open(script, "w").write("import time\ntime.sleep(30)")
    ok, text = local_exec._execute("run_script", script)
    assert not ok
    assert "超时" in text


def test_run_script_failure(sandbox):
    script = os.path.join(sandbox, "bad.py")
    open(script, "w").write("raise SystemExit(3)")
    ok, text = local_exec._execute("run_script", script)
    assert not ok
    assert "exit 3" in text


# ── 白名单与端到端 ─────────────────────────────────────────

def test_try_execute_whitelist_blocked(sandbox):
    outside = tempfile.mkdtemp()
    try:
        handled, text = local_exec.try_execute(f"帮我复制{outside}/a.txt到{sandbox}/b.txt")
        assert handled
        assert "🔒" in text
    finally:
        os.rmdir(outside)


def test_try_execute_copy_end_to_end(sandbox):
    src = os.path.join(sandbox, "a.txt")
    open(src, "w").write("hi")
    dst = os.path.join(sandbox, "b.txt")
    handled, text = local_exec.try_execute(f"帮我复制{src}到{dst}")
    assert handled
    assert os.path.isfile(dst)
    assert "✅ [copy]" in text


def test_collector_file_op_json_target(sandbox):
    from collector.executor import Executor

    src = os.path.join(sandbox, "a.txt")
    open(src, "w").write("hi")
    dst = os.path.join(sandbox, "b.txt")
    ok, text = Executor("http://x", "")._execute("copy", json.dumps([src, dst]))
    assert ok
    assert os.path.isfile(dst)
    assert "已复制" in text
