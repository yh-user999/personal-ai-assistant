"""QQ 插件白名单纯函数测试：不依赖 astrbot 包（main.py 只 import 纯函数）。"""
from pathlib import Path


def _load_pure_functions():
    """只加载 should_handle/can_chat 纯函数（模块级 astrbot import 会失败，用源码截取）。"""
    src = Path(__file__).resolve().parents[2] / "qq" / "astrbot_plugin_xy" / "main.py"
    text = src.read_text(encoding="utf-8")
    start = text.index("def should_handle")
    end = text.index("@register")
    ns: dict = {}
    exec(text[start:end], ns)  # noqa: S102 - 受控源码片段
    return ns["should_handle"], ns["can_chat"]


should_handle, can_chat = _load_pure_functions()


def test_owner_private_chat_allowed():
    assert should_handle("10001", "", owner_qq="10001") is True


def test_group_chat_never_allowed():
    """隐私铁律：即使是主人发言，群聊也一律不处理。"""
    assert should_handle("10001", "12345", owner_qq="10001") is False


def test_stranger_private_chat_denied():
    """主人专属闸门（文件入库）：陌生人不能过。"""
    assert should_handle("99999", "", owner_qq="10001") is False


def test_empty_owner_fail_closed():
    """owner 未配置 = 全拒（fail-closed），不因配置缺失放大权限。"""
    assert should_handle("10001", "", owner_qq="") is False
    assert should_handle("10001", "", owner_qq=None) is False


def test_whitespace_normalized():
    assert should_handle(" 10001 ", "", owner_qq=" 10001 ") is True


# ── v1.4 多人支持：can_chat ─────────────────────────────────

def test_can_chat_stranger_private_allowed():
    """陌生人私聊可聊（v1.4）。"""
    assert can_chat("99999", "", owner_qq="10001") is True


def test_can_chat_owner_private_allowed():
    assert can_chat("10001", "", owner_qq="10001") is True


def test_can_chat_group_never_allowed():
    assert can_chat("10001", "12345", owner_qq="10001") is False
    assert can_chat("99999", "12345", owner_qq="10001") is False


def test_can_chat_owner_missing_fail_closed():
    assert can_chat("99999", "", owner_qq="") is False
    assert can_chat("10001", "", owner_qq=None) is False


def test_can_chat_empty_sender_denied():
    assert can_chat("", "", owner_qq="10001") is False
