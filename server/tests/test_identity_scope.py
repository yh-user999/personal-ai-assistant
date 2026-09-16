"""identity/scope 抽取回归：纯规则、配置适配层和 legacy 导出。"""

import pytest

from app.config import settings
from app.core import memory
from app.domain.identity import (
    group_scope as domain_group_scope,
    is_owner_user as domain_is_owner_user,
    normalize_user_id as domain_normalize_user_id,
    resolve_owner_user_id,
    user_scope as domain_user_scope,
)
from app import identity


def test_domain_identity_rules_preserve_owner_and_guest_semantics():
    assert resolve_owner_user_id("") == "owner"
    assert resolve_owner_user_id(" 42 ") == "42"
    assert domain_normalize_user_id(None, owner_id="") == "owner"
    assert domain_normalize_user_id("  ", owner_id="") == "owner"
    assert domain_normalize_user_id("42", owner_id="42") == "42"
    assert domain_normalize_user_id("7", owner_id="42") == "7"
    assert domain_is_owner_user("42", owner_id="42") is True
    assert domain_is_owner_user("7", owner_id="42") is False


def test_domain_identity_rejects_invalid_guest_ids():
    with pytest.raises(ValueError, match="非法 user_id"):
        domain_normalize_user_id("abc", owner_id="42")
    with pytest.raises(ValueError, match="非法 user_id"):
        domain_normalize_user_id("1" * 13, owner_id="42")


def test_domain_scope_preserves_owner_backfill_and_group_isolation():
    assert domain_user_scope("42", owner_id="42") == ("user_id IN (?, '')", ("42",))
    assert domain_user_scope("7", owner_id="42", col="m.user_id") == (
        "m.user_id = ?",
        ("7",),
    )
    assert domain_group_scope(None) == ("group_id = ?", ("",))
    assert domain_group_scope("9", prefix="m.") == ("m.group_id = ?", ("9",))


def test_legacy_memory_exports_follow_identity_adapter(monkeypatch):
    monkeypatch.setattr(settings, "qq_admin_id", "42")

    assert identity.owner_user_id() == "42"
    assert memory.owner_user_id() == "42"
    assert memory.normalize_user_id(None) == identity.normalize_user_id(None)
    assert memory.normalize_user_id("7") == identity.normalize_user_id("7")
    assert memory.is_owner_user("42") is identity.is_owner_user("42")
    assert memory._user_scope("42") == identity.user_scope("42")
    assert memory._group_scope("9", prefix="m.") == identity.group_scope("9", prefix="m.")
