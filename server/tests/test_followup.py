"""群聊短时追问提示测试。"""
from types import SimpleNamespace

from app.chat import followup
from app.chat.context import ChatResponse


def _ctx(*, is_group=True, message=""):
    return SimpleNamespace(is_group=is_group, message=message)


def test_book_title_question_creates_bounded_hint():
    hint = followup.build_interaction_hint(
        _ctx(),
        SimpleNamespace(needs_clarification=False, social_action="answer"),
        "哪本啊？把书名发我。",
    )

    assert hint == {
        "followup": {"kind": "book_title", "expires_in": 90, "max_messages": 1}
    }


def test_material_request_in_book_context_creates_book_title_hint():
    hint = followup.build_interaction_hint(
        _ctx(message="你觉得某本网络小说怎么样"),
        SimpleNamespace(needs_clarification=False, social_action="answer"),
        "我没有可靠来源，你发一下链接或简介，我再按提供的内容聊。",
    )

    assert hint["followup"]["kind"] == "book_title"


def test_material_request_without_book_context_creates_free_text_hint():
    hint = followup.build_interaction_hint(
        _ctx(message="帮我看看这个方案"),
        SimpleNamespace(needs_clarification=False, social_action="answer"),
        "把链接发我，我再看看。",
    )

    assert hint["followup"]["kind"] == "free_text"


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


def test_semantic_followup_plan_can_open_bounded_window_without_keyword():
    hint = followup.build_interaction_hint(
        _ctx(),
        SimpleNamespace(
            needs_clarification=False,
            social_action="answer",
            followup_required=True,
            followup_kind="free_text",
        ),
        "请补充必要信息。",
    )

    assert hint == {
        "followup": {"kind": "free_text", "expires_in": 90, "max_messages": 1}
    }


def test_directed_group_reply_opens_context_window_without_keyword():
    hint = followup.build_interaction_hint(
        SimpleNamespace(
            is_group=True,
            message="",
            group_directed=True,
            group_context_active=False,
        ),
        SimpleNamespace(
            context_relation="new_topic",
            context_continue=False,
            context_close_reason="",
            reply_decision="answer",
            analysis_only=False,
        ),
        "我先说结论。",
    )

    assert hint["context_window"] == {
        "open": True,
        "expires_in": 90,
        "max_messages": 10,
        "max_noncontinuations": 5,
        "context_relation": "new_topic",
        "context_continue": False,
        "context_close_reason": "",
        "reply_decision": "answer",
        "analysis_only": False,
    }


def test_active_context_can_return_silent_window_decision():
    ctx = _ctx()
    ctx.group_directed = False
    ctx.group_context_active = True
    hint = followup.build_context_window_hint(
        ctx,
        SimpleNamespace(
            context_relation="new_topic",
            context_continue=False,
            context_close_reason="non_continuation",
            reply_decision="ignore",
            analysis_only=True,
        ),
    )

    assert hint["context_window"]["reply_decision"] == "ignore"
    assert hint["context_window"]["analysis_only"] is True
    assert hint["context_window"]["context_close_reason"] == "non_continuation"


def test_directed_no_source_reply_still_carries_context_window_metadata():
    hint = followup.build_interaction_hint(
        SimpleNamespace(
            is_group=True,
            group_directed=True,
            group_context_active=False,
            message="查一下这个",
        ),
        SimpleNamespace(web_no_sources=True, context_relation="new_topic"),
        "暂时无法核实。",
    )
    assert hint["context_window"]["max_messages"] == 10
    assert hint["context_window"]["max_noncontinuations"] == 5
    assert followup.build_interaction_hint(
        _ctx(is_group=False),
        SimpleNamespace(context_relation="new_topic"),
        "我先说结论。",
    ) == {}


def test_chat_response_keeps_legacy_default_shape_compatible():
    response = ChatResponse(reply="ok", memories_used=0)

    assert response.interaction == {}
    assert response.model_dump()["reply"] == "ok"
