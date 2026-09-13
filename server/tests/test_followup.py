"""群聊短时追问提示测试。"""
from types import SimpleNamespace

from app.chat import followup
from app.chat.context import ChatResponse


def _ctx(*, is_group=True):
    return SimpleNamespace(is_group=is_group)


def test_book_title_question_creates_bounded_hint():
    hint = followup.build_interaction_hint(
        _ctx(),
        SimpleNamespace(needs_clarification=False, social_action="answer"),
        "哪本啊？把书名发我。",
    )

    assert hint == {
        "followup": {"kind": "book_title", "expires_in": 90, "max_messages": 1}
    }


def test_non_question_or_private_reply_does_not_open_window():
    assert followup.build_interaction_hint(
        _ctx(is_group=False), SimpleNamespace(needs_clarification=True), "把书名发我。"
    ) == {}
    assert followup.build_interaction_hint(
        _ctx(), SimpleNamespace(needs_clarification=False, social_action="answer"), "好，知道了。"
    ) == {}


def test_explicit_clarification_plan_can_open_free_text_window():
    hint = followup.build_interaction_hint(
        _ctx(),
        SimpleNamespace(needs_clarification=True, social_action="ask_back"),
        "你再具体说说？",
    )

    assert hint["followup"]["kind"] == "free_text"


def test_chat_response_keeps_legacy_default_shape_compatible():
    response = ChatResponse(reply="ok", memories_used=0)

    assert response.interaction == {}
    assert response.model_dump()["reply"] == "ok"
