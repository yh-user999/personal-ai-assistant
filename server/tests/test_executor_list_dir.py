"""执行器 list_dir 排版/排序测试（desktop.local_exec + collector.executor 同格式）。

本地执行器（第 11 课补丁）：自然排序（f2 < f10）、文件夹在前、
隐藏系统项过滤（仅 Windows 生效）、目录不存在报错、空目录提示。
"""
import os
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from desktop import local_exec  # noqa: E402


def _make_tree(path, dirs=(), files=()):
    os.makedirs(path, exist_ok=True)
    for d in dirs:
        os.mkdir(os.path.join(path, d))
    for f in files:
        open(os.path.join(path, f), "w", encoding="utf-8").close()


def test_natural_key():
    names = ["f2", "f10", "f1", "B", "a"]
    assert sorted(names, key=local_exec._natural_key) == ["a", "B", "f1", "f2", "f10"]


def test_list_dir_format_and_sort():
    with tempfile.TemporaryDirectory() as td:
        _make_tree(td, dirs=["zzz", "aaa"], files=["b.txt", "a10.txt", "a2.txt"])
        ok, text = local_exec._execute("list_dir", td)
        assert ok
        lines = text.splitlines()
        assert lines[0] == "共 5 项（📁 2 文件夹 / 📄 3 文件）："
        # 文件夹在前、组内自然排序；文件组内 a2 < a10 < b
        assert lines[1:] == [
            "- 📁 aaa/",
            "- 📁 zzz/",
            "- 📄 a2.txt",
            "- 📄 a10.txt",
            "- 📄 b.txt",
        ]


def test_list_dir_empty():
    with tempfile.TemporaryDirectory() as td:
        ok, text = local_exec._execute("list_dir", td)
        assert ok
        assert text.endswith("\n（空目录）")


def test_list_dir_missing_dir():
    ok, text = local_exec._execute("list_dir", "Z:/不存在的目录xyz")
    assert not ok
    assert "目录不存在" in text


def test_list_dir_truncation():
    with tempfile.TemporaryDirectory() as td:
        _make_tree(td, files=[f"f{i:03d}.txt" for i in range(local_exec.MAX_LIST + 2)])
        ok, text = local_exec._execute("list_dir", td)
        assert ok
        assert f"共 {local_exec.MAX_LIST + 2} 项" in text
        assert f"… 其余 2 项" in text
        # 实际条目行数 = MAX_LIST + 1（… 行）
        assert len(text.splitlines()) == 1 + local_exec.MAX_LIST + 1


def test_hidden_filter_non_windows_is_noop():
    # Linux 测试环境没有 ctypes.windll → 不过滤，函数安全返回 False
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "normal.txt")
        open(p, "w", encoding="utf-8").close()
        assert local_exec._is_hidden_system(p) is False
