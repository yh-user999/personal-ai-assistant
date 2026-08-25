"""自省模块测试：纠正检测 + 教训存取 + 注入。"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")
os.environ.setdefault("DB_PATH", "/tmp/test_self_reflect.db")

from app.models.database import init_db  # noqa: E402
from app.services.self_reflect import (  # noqa: E402
    count_lessons_since,
    detect_correction,
    get_lessons_injection,
    save_lesson,
)


@pytest.fixture(autouse=True)
def fresh_db():
    db_file = Path("/tmp/test_self_reflect.db")
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_file) + suffix).unlink(missing_ok=True)
    init_db()
    yield


def test_detect_correction_signals():
    assert detect_correction("不对，重排序应该放在检索之后")
    assert detect_correction("你错了，我说的是向量库")
    assert detect_correction("记住，我更喜欢简洁的回答")
    assert detect_correction("以后别用这种格式")
    assert not detect_correction("今天天气怎么样")
    assert not detect_correction("帮我看看这个报错")


def test_save_and_inject_lessons():
    save_lesson("重排序应该放在检索之后", "AI 说：重排序放在检索前")
    save_lesson("回答要简洁，不要长篇大论", "")
    text = get_lessons_injection()
    assert "重排序应该放在检索之后" in text
    assert "回答要简洁" in text


def test_count_lessons_since():
    save_lesson("测试教训")
    # 从远古时间起统计应有 1 条
    assert count_lessons_since("2000-01-01T00:00:00+00:00") >= 1
    # 从未来时间起统计应为 0
    assert count_lessons_since("2100-01-01T00:00:00+00:00") == 0
