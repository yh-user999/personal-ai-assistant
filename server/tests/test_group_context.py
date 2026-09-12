"""群聊短期上下文测试：能接住追问，但绝不落库、不建群成员档案。"""
import asyncio

import pytest

from app.chat import group_context
from app.chat.context import ChatContext, ChatRequest
from app.chat.retrieval import retrieve
from app.config import settings
from app.models.database import connect, init_db, reset_connections


@pytest.fixture(autouse=True)
def clean_store():
    group_context.clear()
    yield
    group_context.clear()


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    reset_connections()
    init_db()
    yield
    reset_connections()


def make_ctx(message, group_id="456", uid="10086"):
    return ChatContext(
        request=type("R", (), {"state": type("S", (), {})()})(),
        request_model=ChatRequest(message=message, user_id=uid, group_id=group_id),
        message=message,
        uid=uid,
        is_owner=False,
        group_id=group_id,
    )


# ── 上下文可用性 ───────────────────────────────────────────

def test_recent_messages_returns_context_in_order():
    group_context.remember("456", "user", "帮我推荐本书", user_id="10086")
    group_context.remember("456", "assistant", "看什么类型的？")
    group_context.remember("456", "user", "科幻", user_id="10086")

    items = group_context.recent_messages("456")
    assert [i["role"] for i in items] == ["user", "assistant", "user"]
    assert items[1]["content"] == "看什么类型的？"
    assert items[2]["content"].endswith("科幻")


def test_group_retrieve_includes_recent_context():
    group_context.remember("456", "user", "推荐本书", user_id="10086")
    group_context.remember("456", "assistant", "看什么类型的？")

    runtime = type("Runtime", (), {
        "settings": settings, "memory": object(), "knowledge": object(), "services": object(),
    })()
    bundle = asyncio.run(retrieve(make_ctx("科幻"), runtime, None))

    assert len(bundle.history) == 2
    # 仍不得引入个人记忆或证据
    assert bundle.mems == [] and bundle.evidence == {}


# ── 隔离边界（核心隐私保证）─────────────────────────────────

def test_group_context_never_touches_database(db_env):
    """群上下文必须只在内存：数据库不得出现任何群消息。"""
    for i in range(4):
        group_context.remember("456", "user", f"群成员发言{i}", user_id=f"1000{i}")
        group_context.remember("456", "assistant", f"回复{i}")

    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
        hits = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE content LIKE '%群成员发言%'"
        ).fetchone()[0]
        assert hits == 0, "群消息不得写入可检索的记忆表"
    finally:
        conn.close()


def test_speaker_alias_hides_real_identity():
    """发言人只保留短别名，不得泄露 QQ 号。"""
    qq = "1958704488"
    alias = group_context.speaker_alias(qq)
    assert qq not in alias
    assert alias.startswith("成员") and len(alias) <= 10
    # 同一人稳定、不同人可区分
    assert alias == group_context.speaker_alias(qq)
    assert alias != group_context.speaker_alias("123456")


def test_context_is_isolated_between_groups():
    group_context.remember("456", "user", "A群的话", user_id="1")
    group_context.remember("789", "user", "B群的话", user_id="2")

    a = " ".join(i["content"] for i in group_context.recent_messages("456"))
    b = " ".join(i["content"] for i in group_context.recent_messages("789"))
    assert "A群的话" in a and "B群的话" not in a
    assert "B群的话" in b and "A群的话" not in b


# ── 有界性：不会无限累积 ────────────────────────────────────

def test_only_recent_turns_are_kept():
    for i in range(group_context.MAX_TURNS_PER_GROUP + 6):
        group_context.remember("456", "user", f"第{i}句", user_id="1")

    items = group_context.recent_messages("456")
    assert len(items) == group_context.MAX_TURNS_PER_GROUP
    assert "第0句" not in " ".join(i["content"] for i in items)


def test_expired_context_is_dropped():
    now = 1000.0
    group_context.remember("456", "user", "很久以前的话", user_id="1", now=now)
    assert group_context.recent_messages("456", now=now) != []
    # 超过 TTL 后视为新话题
    later = now + group_context.TTL_SECONDS + 1
    assert group_context.recent_messages("456", now=later) == []


def test_group_count_is_bounded():
    for i in range(group_context.MAX_GROUPS + 10):
        group_context.remember(str(900000 + i), "user", "话", user_id="1")
    assert group_context.tracked_groups() <= group_context.MAX_GROUPS


def test_long_message_is_truncated():
    group_context.remember("456", "user", "长" * 2000, user_id="1")
    content = group_context.recent_messages("456")[0]["content"]
    assert len(content) <= group_context.MAX_CHARS_PER_ITEM + 12
