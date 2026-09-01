"""执行器绕过与误吞回归测试（本轮审计发现的问题）。

三类问题各自都曾在生产代码里成立，全部实测复现过：
1. 扩展名黑名单被尾点/尾空格/NTFS 数据流绕过
2. 快捷启动器模糊匹配劫持 open，绕过白名单与扩展名黑名单
3. 命令正则把正常聊天误判为文件操作（10 条实测 10 条全中）
sys.path 与测试环境由 tests/conftest.py 统一注入。
"""
import os

import pytest

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

from common import launcher
from common.file_ops import exec_ext, is_blocked_open

from app.services import executor as srv


# ── ① 扩展名黑名单的变形绕过 ──────────────────────────────
# Windows 打开文件时会剥掉结尾的点与空格，NTFS 备用数据流（x.bat::$DATA）
# 同样照常执行主文件——裸 os.path.splitext 对这些变形全部漏判。

BYPASS_VARIANTS = [
    "F:/x.bat",           # 基线
    "F:/x.bat.",          # 尾点 → 旧实现得 '.'
    "F:/x.bat  ",         # 尾空格 → 旧实现得 '.bat  '
    "F:/x.bat...  ",      # 混合
    "F:/x.bat::$DATA",    # NTFS 备用数据流
    "F:/x.bat:extra",     # 单冒号流名
    "F:/x.BAT",           # 大小写
    "F:/setup.exe.",
    "F:/a.ps1.",
    "F:/a.vbs ",
]


@pytest.mark.parametrize("target", BYPASS_VARIANTS)
def test_shared_blocklist_catches_bypass_variants(target):
    assert is_blocked_open(target) is True, f"{target} 应被拦截"


@pytest.mark.parametrize("target", BYPASS_VARIANTS)
def test_server_precheck_catches_bypass_variants(target):
    """服务端预检与共享判据必须同步拦截（两处实现，语义必须一致）。"""
    assert srv.check_open_target(target) is False, f"{target} 服务端应拒绝"


@pytest.mark.parametrize("target", [
    "F:/doc.txt", "F:/notes.md", "F:/report.pdf",
    "C:/Users/me/Documents", "F:/", "微信",
])
def test_normal_targets_still_allowed(target):
    assert is_blocked_open(target) is False
    assert srv.check_open_target(target) is True


def test_two_implementations_agree_on_extension():
    """server 的 _exec_ext 与 common 的 exec_ext 必须给出相同结果。

    服务端跑在 Linux 且不把仓库根放进 sys.path，无法 import common，
    因此保留了一份等价实现——这个测试就是防它们漂移的锁。
    """
    for t in BYPASS_VARIANTS + ["F:/a.txt", "F:/x", "", "F:/a.tar.gz"]:
        assert srv._exec_ext(t) == exec_ext(t), t


# ── ② 启动器模糊匹配劫持 open ─────────────────────────────

@pytest.fixture
def registered_launcher(tmp_path, monkeypatch):
    """注册一个指向白名单外路径的应用（这是设计允许的显式授权）。"""
    store = tmp_path / "launcher.json"
    monkeypatch.setenv("LAUNCHER_STORE", str(store))
    launcher.save({
        "version": 1,
        "items": {
            "微信": {"alias": "微信", "app": "D:/Program/WeChat/WeChat.exe", "use_count": 5},
            "b站": {"alias": "B站", "url": "https://www.bilibili.com", "use_count": 1},
        },
        "browsers": {},
    })
    return store


def test_exact_alias_still_resolves(registered_launcher):
    """收紧模糊匹配不能损坏正常用法。"""
    assert launcher.find_item("微信", want="open", strict=True)["alias"] == "微信"
    assert launcher.find_item("b站", want="open", strict=True)["alias"] == "B站"
    assert launcher.find_item("B站", want="open", strict=True)["alias"] == "B站"


@pytest.mark.parametrize("target", ["微", "信", "b", "站"])
def test_single_char_no_longer_hijacks_open(target, registered_launcher):
    """单字符子串曾命中已注册应用 → 等于任意启动白名单外 exe。"""
    assert launcher.find_item(target, want="open", strict=True) is None


@pytest.mark.parametrize("target", [
    "F:/我的微信资料",
    "C:/temp/微信备份",
    "/mnt/data/b站素材",
    "D:\\work\\微信导出",
])
def test_path_shaped_target_not_hijacked(target, registered_launcher):
    """用户给了明确路径时必须走白名单校验，不能被别名劫持成启动 exe。

    这是最隐蔽的一例：'F:/我的微信资料' 含"微信"子串，旧实现会去
    os.startfile(WeChat.exe)，既没打开那个目录、又绕过了白名单。
    """
    assert launcher.find_item(target, want="open", strict=True) is None


def test_search_template_lookup_stays_lenient(tmp_path, monkeypatch):
    """搜索模板通道不收紧（不涉及启动可执行文件，易用性优先）。"""
    monkeypatch.setenv("LAUNCHER_STORE", str(tmp_path / "l.json"))
    launcher.save({
        "version": 1,
        "items": {"b站": {"alias": "B站", "template": "https://s.b.com/?w={q}", "use_count": 1}},
        "browsers": {},
    })
    assert launcher.find_item("b", want="template") is not None


# ── ③ 命令正则误吞正常聊天 ────────────────────────────────

CHAT_NOT_COMMANDS = [
    "打开心结吧",
    "看看今天天气怎么样",
    "把这段话改成更正式的语气",
    "读一下这段代码有什么问题",
    "帮我看看这个方案行不行",
    "查看一下项目进展",
    "备份策略到底怎么设计比较好",
    "复制这段文字到剪贴板",
    "移动重心到左脚",
    "把变量名改成 userName",
    "完成目标需要哪些步骤",
    "看看有没有更好的办法",
]


@pytest.mark.parametrize("msg", CHAT_NOT_COMMANDS)
def test_chat_not_parsed_as_file_command(msg):
    """正常聊天不该被解析成文件操作。

    未被拦住的少数 open 形态（形态与真别名无法区分）由 chat 层的
    plausible_open_target + 确认层兜住，见 test_confirm.py。
    """
    parsed = srv.parse_executor_command(msg)
    if parsed is None:
        return
    action, target = parsed
    assert action == "open", f"{msg} 被误判为 {action}"
    # 落到 open 的，必须是"低置信度"从而触发确认，而不是直接执行
    assert not srv.confident_open_target(target), f"{msg} 会被直接执行"


REAL_COMMANDS = [
    ("打开F盘", "open"),
    ("打开 F:/projects", "open"),
    ("打开微信", "open"),
    ("看看F:/projects目录有什么", "list_dir"),
    ("读一下 F:/notes/todo.md", "read_file"),
    ("复制F:/a.txt到F:/backup/", "copy"),
    ("备份F:/项目到F:/backup", "backup"),
    ("把F:/a.txt移动到F:/old/", "move"),
    ("重命名F:/a.txt为b.txt", "rename"),
    ("帮我找F:/projects里的todo文件", "search_files"),
]


@pytest.mark.parametrize("msg,expected", REAL_COMMANDS)
def test_real_commands_still_parsed(msg, expected):
    """收紧闸门不能造成漏判——真实指令必须照旧工作。"""
    parsed = srv.parse_executor_command(msg)
    assert parsed is not None, f"{msg} 被漏判"
    assert parsed[0] == expected


def test_destructive_pair_requires_path_evidence():
    """双路径动作至少一侧要像真实路径。"""
    assert srv._valid_pair("F:/a.txt", "b.txt") is True
    assert srv._valid_pair("这段话", "更正式的语气") is False
    assert srv._valid_pair("策略", "底怎么设计比较好") is False


def test_confident_open_target_classification():
    """常见别名免确认，长中文短语要确认。"""
    for high in ["微信", "钉钉", "B站", "VSCode", "QQ音乐", "F:/", "F:/projects"]:
        assert srv.confident_open_target(high) is True, high
    for low in ["新世界的大门", "思路想想别的办法", "心结吧那个话题"]:
        assert srv.confident_open_target(low) is False, low
