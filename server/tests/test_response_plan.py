from app.chat.providers import calculator, current_datetime
from app.chat.response_plan import build_rule_plan


def test_datetime_synonyms_use_direct_fact_plan():
    for text in ("今天周几", "今天礼拜几", "当前日期是什么", "现在是几号"):
        plan = build_rule_plan(text)
        assert plan.mode == "direct_fact"
        assert plan.intent == "current_datetime"
        assert current_datetime(text)["value"]["timezone"] == "Asia/Shanghai"


def test_other_response_modes():
    assert build_rule_plan("我之前说过李羽什么设定").mode == "retrieve_then_answer"
    assert build_rule_plan("这个架构有什么问题").mode == "reasoning"
    assert build_rule_plan("继续写第三章").mode == "creative"
    assert build_rule_plan("我今天很累，什么都不想做").mode == "emotional_support"


def test_safe_calculator_rejects_code():
    assert calculator("2 + 3 * 4")["value"] == 14
    assert calculator("__import__('os').system('id')") is None
    assert calculator("2 ** 100") is None
