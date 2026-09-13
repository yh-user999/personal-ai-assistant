from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "set-global-group-chat.py"
_spec = importlib.util.spec_from_file_location("set_global_group_chat", MODULE_PATH)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)


SOURCE = '''
[napcat_server]
host = "napcat.test"
port = 3001

[chat]
enable_chat_list_filter = true
show_dropped_chat_list_messages = false
group_list_type = "whitelist"
group_list = ["group-fixture"]
private_list_type = "whitelist"
private_list = []
ban_user_id = []

[notice]
enabled = true
'''.lstrip()


def test_rewrite_preserves_other_sections_and_private_boundary() -> None:
    updated = _module.rewrite_group_policy(SOURCE)
    data = tomllib.loads(updated)

    assert data["chat"]["enable_chat_list_filter"] is True
    assert data["chat"]["group_list_type"] == "blacklist"
    assert data["chat"]["group_list"] == []
    assert data["chat"]["private_list_type"] == "whitelist"
    assert data["chat"]["private_list"] == []
    assert data["napcat_server"] == {"host": "napcat.test", "port": 3001}
    assert data["notice"]["enabled"] is True
    _module.validate_group_policy(updated)


def test_rewrite_is_idempotent() -> None:
    updated = _module.rewrite_group_policy(SOURCE)
    assert _module.rewrite_group_policy(updated) == updated


def test_missing_target_key_fails_closed() -> None:
    with pytest.raises(_module.ConfigError, match="缺少配置键"):
        _module.rewrite_group_policy(SOURCE.replace('group_list = ["group-fixture"]\n', ""))


def test_summary_does_not_print_identifiers() -> None:
    text = SOURCE.replace('group_list = ["group-fixture"]', 'group_list = ["group-fixture", "second-fixture"]')
    summary = _module.summarize(text)
    assert "group_count=2" in summary
    assert "group-fixture" not in summary
    assert "second-fixture" not in summary
    assert "private_count=0" in summary
