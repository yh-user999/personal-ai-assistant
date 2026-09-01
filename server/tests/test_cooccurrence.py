"""共现扩散测试：数据量门控、一跳关联、泛话题过滤、用户隔离。

核心设计是"数据不够就不做"——共现图在稀疏数据上建不出可靠的边，
实测本地 719 条记忆里只有 9 条有 topics、22 个话题各出现 1 次。
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

from app.models.database import connect  # noqa: E402
from app.services import cooccurrence as co  # noqa: E402


def _seed(topics: list[str], content: str = "x", user_id: str = "owner") -> int:
    conn = connect()
    cur = conn.execute(
        "INSERT INTO memories (user_id, sender, content, topics, ts) "
        "VALUES (?, 'user', ?, ?, '2026-09-01T00:00:00+00:00')",
        (user_id, content, json.dumps(topics, ensure_ascii=False)),
    )
    conn.commit()
    return cur.lastrowid


def _lower_thresholds(monkeypatch, tagged=2, shared=1):
    monkeypatch.setattr(co, "MIN_TAGGED_MEMORIES", tagged)
    monkeypatch.setattr(co, "MIN_SHARED_TOPICS", shared)


# ── 门控：数据不足时不做扩散 ────────────────────────────────

def test_disabled_on_sparse_data(db):
    """默认门槛下小数据必须关闭（生产库实测就是这个状态）。"""
    for i in range(5):
        _seed([f"话题{i}"])
    ok, why = co.is_enabled()
    assert ok is False
    assert "记忆" in why or "话题" in why


def test_expand_noop_when_disabled(db):
    mid = _seed(["跳槽"])
    hits = [{"id": mid, "content": "x", "score": 0.8}]
    assert co.expand(hits) == hits, "门控未通过时应原样返回"


def test_enabled_when_thresholds_met(db, monkeypatch):
    _lower_thresholds(monkeypatch)
    a = _seed(["跳槽", "薪资"])
    _seed(["跳槽", "通勤"])
    assert a
    assert co.is_enabled()[0] is True


# ── 一跳关联：语义不相近但同期出现 ─────────────────────────

def test_expands_via_shared_topic(db, monkeypatch):
    """问跳槽时，把只记了"薪资对比"的那条也带出来。"""
    _lower_thresholds(monkeypatch)
    hit = _seed(["跳槽"], "上次跳槽的打算")
    related = _seed(["跳槽"], "薪资对比：A 家高 15%")
    out = co.expand([{"id": hit, "content": "上次跳槽的打算", "score": 0.8}])
    ids = [m["id"] for m in out]
    assert related in ids, "共现记忆未被补充"
    extra = next(m for m in out if m["id"] == related)
    assert extra["via_cooccurrence"] is True
    assert extra["score"] < 0.8, "扩散来的分数必须低于直接命中"


def test_does_not_duplicate_hits(db, monkeypatch):
    _lower_thresholds(monkeypatch)
    a = _seed(["跳槽"])
    b = _seed(["跳槽"])
    out = co.expand([
        {"id": a, "content": "x", "score": 0.9},
        {"id": b, "content": "y", "score": 0.7},
    ])
    ids = [m["id"] for m in out]
    assert len(ids) == len(set(ids)), "已命中的记忆不该被重复补充"


def test_broad_topic_skipped(db, monkeypatch):
    """「日常闲聊」这类泛话题连一大片记忆，用它连边会稀释信号。"""
    _lower_thresholds(monkeypatch)
    monkeypatch.setattr(co, "MAX_MEMORIES_PER_TOPIC", 3)
    hit = _seed(["日常闲聊"])
    for i in range(6):
        _seed(["日常闲聊"], f"闲聊{i}")
    out = co.expand([{"id": hit, "content": "x", "score": 0.8}])
    assert len(out) == 1, "泛话题不应触发扩散"


def test_respects_top_k(db, monkeypatch):
    _lower_thresholds(monkeypatch)
    hit = _seed(["跳槽"])
    for i in range(8):
        _seed(["跳槽"], f"相关{i}")
    out = co.expand([{"id": hit, "content": "x", "score": 0.8}], top_k=2)
    assert len(out) == 3, "1 条命中 + 2 条扩散"


# ── 隔离与健壮性 ──────────────────────────────────────────

def test_user_isolation(db, monkeypatch):
    """访客的记忆不能被扩散进主人的上下文。"""
    _lower_thresholds(monkeypatch)
    hit = _seed(["跳槽"], "主人的", user_id="owner")
    guest = _seed(["跳槽"], "访客的", user_id="123456")
    out = co.expand([{"id": hit, "content": "x", "score": 0.8}], user_id="owner")
    assert guest not in [m["id"] for m in out]


def test_empty_hits(db):
    assert co.expand([]) == []


def test_malformed_topics_tolerated(db, monkeypatch):
    _lower_thresholds(monkeypatch)
    conn = connect()
    conn.execute(
        "INSERT INTO memories (user_id, sender, content, topics, ts) "
        "VALUES ('owner','user','x','not-json','2026-09-01T00:00:00+00:00')"
    )
    conn.commit()
    hit = _seed(["跳槽"])
    co.expand([{"id": hit, "content": "x", "score": 0.8}])  # 不该抛异常
