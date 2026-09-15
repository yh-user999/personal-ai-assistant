"""群聊注意力漂移和自然表达配置测试。"""
from types import SimpleNamespace

from app.group import attention as attention_drift


def _settings(**values):
    defaults = {
        "group_heartflow_enabled": True,
        "group_drift_level": "subtle",
        "group_anchor_policy": "strict",
        "group_reaction_style": "reserved",
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_default_block_is_conservative_and_requires_anchor():
    block = attention_drift.build_prompt_block(_settings())

    assert "轻轻联想一句" in block
    assert "立刻回到当前问题" in block
    assert "不要凭空补充事实" in block


def test_active_natural_profile_changes_only_expression_rules():
    block = attention_drift.build_prompt_block(
        _settings(
            group_drift_level="active",
            group_anchor_policy="balanced",
            group_reaction_style="natural",
        )
    )

    assert "抓住新鲜、好笑" in block
    assert "沿支线说一句" in block
    assert "偶尔先用短句" in block
    assert "不要凭空补充事实" in block


def test_invalid_values_fall_back_to_safe_defaults():
    values = attention_drift.normalize_settings(
        _settings(
            group_drift_level="wild",
            group_anchor_policy="loose",
            group_reaction_style="lively",
        )
    )

    assert values == {
        "drift_level": "subtle",
        "anchor_policy": "strict",
        "reaction_style": "reserved",
    }


def test_disabled_heartflow_does_not_inject_drift_rules():
    assert attention_drift.build_prompt_block(_settings(group_heartflow_enabled=False)) == ""
