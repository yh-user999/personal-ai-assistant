"""群记忆与私聊记忆的隔离铁律。

这是本次改造的安全网：memories 表加了 group_id 后，任何一处检索漏加群条件，
都会让同一个人在群里说的话串进他的私聊上下文（或反向）。这里逐个入口验证。
"""
import asyncio

import pytest

from app.config import settings
from app.core import memory as memory_module
from app.models.database import connect, init_db, reset_connections

GUEST = "10086"
GROUP = "916000001"
OTHER_GROUP = "916000002"


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    reset_connections()
    init_db()

    import app.core.embedding as _embedding

    async def fake_embed(texts):
        return [[0.0] * settings.embedding_dimension for _ in texts]

    monkeypatch.setattr(_embedding, "embed", fake_embed)
    yield
    reset_connections()


def _seed(db_env=None):
    """同一用户分别在私聊、A 群、B 群各留一条消息。"""
    asyncio.run(memory_module.write_message("user", "私聊里的秘密内容", user_id=GUEST))
    asyncio.run(memory_module.write_message("user", "A群里的公开发言", user_id=GUEST, group_id=GROUP))
    asyncio.run(memory_module.write_message("user", "B群里的另一段话", user_id=GUEST, group_id=OTHER_GROUP))


# ── 写入带上群标记 ─────────────────────────────────────────

def test_group_id_is_persisted(db_env):
    _seed()
    conn = connect()
    try:
        rows = dict(
            conn.execute(
                "SELECT content, group_id FROM memories WHERE user_id=?", (GUEST,)
            ).fetchall()
        )
    finally:
        conn.close()
    assert rows["私聊里的秘密内容"] == ""
    assert rows["A群里的公开发言"] == GROUP
    assert rows["B群里的另一段话"] == OTHER_GROUP


# ── 历史读取隔离 ───────────────────────────────────────────

def test_recent_history_is_scoped(db_env):
    _seed()
    private = " ".join(i["content"] for i in memory_module.get_recent_history(20, user_id=GUEST))
    in_group = " ".join(
        i["content"] for i in memory_module.get_recent_history(20, user_id=GUEST, group_id=GROUP)
    )

    assert "私聊里的秘密" in private
    assert "A群" not in private and "B群" not in private, "私聊历史不得混入群消息"

    assert "A群" in in_group
    assert "私聊里的秘密" not in in_group, "群历史不得泄漏私聊内容"
    assert "B群" not in in_group, "群历史不得跨群"


# ── 检索（向量/关键词）隔离 ─────────────────────────────────

def test_search_is_scoped(db_env):
    _seed()

    private = asyncio.run(memory_module.search("内容 发言 话", user_id=GUEST))
    assert all("群里" not in m["content"] for m in private), "私聊检索不得命中群消息"

    grouped = asyncio.run(memory_module.search("内容 发言 话", user_id=GUEST, group_id=GROUP))
    assert all("私聊里的秘密" not in m["content"] for m in grouped), "群检索不得命中私聊"
    assert all("B群" not in m["content"] for m in grouped), "群检索不得跨群"


def test_keyword_search_is_scoped(db_env):
    """FTS 全文检索同样必须按群隔离。"""
    _seed()
    rows = memory_module.deep_keyword_search("发言", user_id=GUEST, group_id=GROUP)
    assert all("私聊" not in r["content"] for r in rows)

    private_rows = memory_module.deep_keyword_search("秘密", user_id=GUEST)
    assert all("群里" not in r["content"] for r in private_rows)


# ── 群消息不污染个人长期数据 ────────────────────────────────

def test_group_history_is_retrievable_after_context_expires(db_env):
    """本次改造的目标：内存上下文失效后，仍能从库里检索到本群历史。"""
    from app.group import context as group_context

    asyncio.run(memory_module.write_message(
        "user", "上周我们聊过量子计算的前景", user_id=GUEST, group_id=GROUP
    ))
    group_context.clear()  # 模拟重启/超时，内存上下文全丢

    hits = asyncio.run(memory_module.search("量子计算", user_id=GUEST, group_id=GROUP))
    assert any("量子计算" in m["content"] for m in hits), "群历史应可检索"

    # 同一查询在私聊作用域下必须查不到
    private = asyncio.run(memory_module.search("量子计算", user_id=GUEST))
    assert all("量子计算" not in m["content"] for m in private)


def test_group_messages_excluded_from_older_summaries(db_env):
    _seed()
    older = memory_module.get_older_summaries(window_size=1, user_id=GUEST)
    assert all("群里" not in text for text in older), "更早摘要不得包含群消息"


# ── 排序权重也必须同作用域 ──────────────────────────────────

def test_topic_boost_is_scoped_to_current_conversation(db_env):
    """话题活跃度补偿只能统计当前作用域的话题。

    这里是排序权重而不是返回内容：漏加群条件不会泄漏正文，但会让群聊话题
    污染私聊检索排序（反向亦然），违反群作用域不注入私聊数据的约定。
    """
    asyncio.run(memory_module.write_message("user", "私聊话题", user_id=GUEST))
    asyncio.run(memory_module.write_message("user", "群聊话题", user_id=GUEST, group_id=GROUP))

    conn = connect()
    try:
        conn.execute(
            "UPDATE memories SET topics=? WHERE group_id='' AND user_id=?",
            ('["私聊专属话题"]', GUEST),
        )
        conn.execute(
            "UPDATE memories SET topics=? WHERE group_id=? AND user_id=?",
            ('["群专属话题"]', GROUP, GUEST),
        )
        conn.commit()

        private = memory_module._topic_boost_map(conn, user_id=GUEST)
        in_group = memory_module._topic_boost_map(conn, user_id=GUEST, group_id=GROUP)
        other_group = memory_module._topic_boost_map(conn, user_id=GUEST, group_id=OTHER_GROUP)
    finally:
        conn.close()

    assert private["私聊专属话题"] == 1
    assert private["群专属话题"] == 0, "私聊热点补偿不得统计群话题"
    assert in_group["群专属话题"] == 1
    assert in_group["私聊专属话题"] == 0, "群热点补偿不得统计私聊话题"
    assert other_group["群专属话题"] == 0, "其他群的话题不得互相影响"
