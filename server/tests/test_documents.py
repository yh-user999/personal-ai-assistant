"""文档生成服务测试：命令解析 + 存取。"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")
os.environ.setdefault("DB_PATH", "/tmp/test_documents.db")

from app.models.database import connect, init_db  # noqa: E402
from app.services.documents import (  # noqa: E402
    get_document,
    list_documents,
    parse_doc_command,
)


@pytest.fixture(autouse=True)
def fresh_db():
    init_db()
    conn = connect()
    conn.execute("DELETE FROM documents")
    conn.commit()
    conn.close()
    yield


def test_parse_full_command():
    title, req = parse_doc_command("写文档：标题RAG调优总结，内容：总结本周调优工作")
    assert title == "RAG调优总结"
    assert "调优" in req


def test_parse_title_only():
    title, req = parse_doc_command("写文档 周报草稿")
    assert title == "周报草稿"
    assert req == "周报草稿"


def test_parse_not_command():
    assert parse_doc_command("帮我看看这个报错") is None
    assert parse_doc_command("写代码实现排序") is None


def test_list_and_get_empty():
    assert list_documents() == []
    assert get_document(999) is None
