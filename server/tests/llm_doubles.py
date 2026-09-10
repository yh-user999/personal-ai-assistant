"""内部 LLM 调用的测试桩约定。

自主响应 planner 与回复审校都会额外调用 ``llm.chat``，并带 ``purpose`` 标记
（planner / review / revise）。测试如果要断言"发给回复模型的 messages"，
必须先让这些内部调用短路返回桩响应；否则捕获到的会是规划或审校请求，
断言会以难以定位的方式失败。

桩响应刻意取"低置信 + 无需修订"，使被测链路退回最朴素的行为，
不对断言产生额外影响。
"""

PLANNER_STUB = '{"mode": "casual_chat", "confidence": 0.0, "intent": "stub"}'
# 全项满分 + 无需修订：审校一律通过，不触发重写，对断言零干扰。
# 分数必须给全（不能留空 dict）：留空会被判成各维度 0 分，
# 而 safety<0.95 是硬门槛，会直接触发一次重写。
REVIEW_STUB = (
    '{"needs_revision": false, "scores": {"relevance": 1.0, "state_fit": 1.0, '
    '"grounding": 1.0, "tone": 1.0, "brevity": 1.0, "safety": 1.0, '
    '"stance_clarity": 1.0, "noise_resistance": 1.0, "proportionality": 1.0}, '
    '"issues": [], "revision_plan": [], "confidence": 1.0}'
)
# 重写返回空串：调用方 `final or draft` 会保留原草稿，等于把重写中和掉。
# 若这里也返回 JSON，那段 JSON 会被当成最终回复返回给用户。
REVISE_STUB = ""

INTERNAL_PURPOSES = ("planner", "review", "revise")


def internal_call_response(kwargs: dict) -> str | None:
    """内部调用返回桩响应；真正的回复调用返回 None（由调用方继续处理）。"""
    purpose = kwargs.get("purpose")
    if purpose == "planner":
        return PLANNER_STUB
    if purpose == "review":
        return REVIEW_STUB
    if purpose == "revise":
        return REVISE_STUB
    return None
