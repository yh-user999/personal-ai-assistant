"""第 6.21 课测试：事实自动提取（JSON 容错解析 + upsert 去重）。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

import pytest

from app.config import settings
from app.models.database import connect, init_db, reset_connections
from app.services.fact_extract import parse_facts_json, upsert_facts


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """独立临时库。原实现靠 DB_PATH 环境变量隔离（无效），
    test_upsert_dedup 里的 DELETE FROM facts 一直跑在生产库上。"""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    reset_connections()
    init_db()
    yield
    reset_connections()


def test_parse_clean_json():
    text = '[{"subject":"李羽","predicate":"能力","object":"杀人则变强"}]'
    assert parse_facts_json(text) == [
        {"subject": "李羽", "predicate": "能力", "object": "杀人则变强"}
    ]


def test_parse_noisy_wrapper():
    text = '提取结果如下：\n[{"subject":"李羽","predicate":"性格底色","object":"正直的绝对主义者"}] 以上。'
    triples = parse_facts_json(text)
    assert len(triples) == 1
    assert triples[0]["subject"] == "李羽"


def test_parse_empty_and_bad():
    assert parse_facts_json("[]") == []
    assert parse_facts_json("没有可提取内容") == []
    assert parse_facts_json('[{"subject":""}]') == []  # 空字段被过滤


def test_upsert_dedup():
    # 库隔离与建表由 autouse 的 fresh_db 负责（临时库本就是空的）
    upsert_facts([{"subject": "李羽", "predicate": "能力", "object": "杀人变强"}])
    # 同 subject+predicate 再写入 → 覆盖不新增
    upsert_facts([{"subject": "李羽", "predicate": "能力", "object": "杀人则变强，全面提升"}])
    conn = connect()
    rows = conn.execute("SELECT object FROM facts WHERE subject='李羽'").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["object"] == "杀人则变强，全面提升"
