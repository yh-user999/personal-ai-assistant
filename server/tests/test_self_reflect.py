"""自省模块测试：纠正检测 + 教训存取 + 注入。"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

from app.config import settings
from app.models.database import connect, init_db, reset_connections
from app.services.self_reflect import (
    classify_lesson,
    count_lessons_since,
    detect_correction,
    get_lessons_injection,
    save_lesson,
)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """每个用例一个独立临时库。

    必须 monkeypatch settings.db_path，不能靠 os.environ.setdefault("DB_PATH")：
    conftest 在收集阶段就 import 了 app.config，settings 是 lru_cache 单例，
    此后改环境变量已经无效——那种写法会让测试静默写到真实库
    ./data/assistant.db（本文件的迁移用例含 DROP TABLE，曾真删掉线上教训表）。
    """
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    reset_connections()  # 长驻连接缓存握着旧库句柄，切库后必须丢弃
    init_db()
    yield
    reset_connections()


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


# ── 去重与优先级（实测 bug 修复：52 行里只有 7 条不同内容）────

def test_save_lesson_dedupes_same_content():
    """同一句纠正反复存只留一行——否则注入窗口被副本占满。"""
    first = save_lesson("回答要简洁，不要长篇大论", "ctx1")
    for _ in range(15):
        save_lesson("回答要简洁，不要长篇大论", "ctx2")
    conn = connect()
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM lessons WHERE content='回答要简洁，不要长篇大论'"
    ).fetchone()["c"]
    ctx = conn.execute(
        "SELECT context FROM lessons WHERE content='回答要简洁，不要长篇大论'"
    ).fetchone()["context"]
    conn.close()
    assert n == 1
    assert ctx == "ctx2", "重复纠正应刷新 context 与时间"
    assert save_lesson("回答要简洁，不要长篇大论") == first, "id 应保持稳定"


def test_injection_has_no_duplicates():
    for _ in range(10):
        save_lesson("重排序应该放在检索之后")
        save_lesson("回答要简洁，不要长篇大论")
    lines = get_lessons_injection().splitlines()
    assert len(lines) == len(set(lines)), f"注入出现重复行: {lines}"


def test_classify_lesson_kinds():
    assert classify_lesson("你就叫小月吧，记住了吗") == "identity"
    assert classify_lesson("给你起名叫小月") == "identity"
    assert classify_lesson("回答要简洁，不要长篇大论") == "style"
    assert classify_lesson("以后别用 emoji") == "style"
    assert classify_lesson("不对，重排序应该放在检索之后") == "fact"


@pytest.mark.parametrize("text", [
    "还记得你的名字吗",
    "你叫什么名字",
    "你的名字是什么？",
    "你还记得吗",
    "你知道你叫什么吗",
])
def test_questions_are_not_identity(text):
    """提问身份 ≠ 定义身份。

    实测线上把"还记得你的名字吗"存成了 identity 并永久最高优先注入——
    一句提问占住了人格锚点的位置。
    """
    assert classify_lesson(text) != "identity", f"问句被误判为身份设定: {text}"


@pytest.mark.parametrize("text", [
    "你就叫小月吧，记住了吗",   # 带确认尾缀但确实是命名
    "给你起名叫小雪",
    "以后你就是我的助手",
    "你的名字是小月",
    "就叫你小月",
])
def test_naming_statements_are_identity(text):
    """命名句常带"记住了吗"这类确认尾缀，不能因此被当成提问。"""
    assert classify_lesson(text) == "identity", f"命名未被识别: {text}"


def test_detect_correction_covers_ni_jiu_jiao():
    """"你就叫X"原先漏在信号词外，整句检测不到。"""
    assert detect_correction("你就叫小狗吧")
    assert detect_correction("你就叫小月吧")


def test_injection_records_hit_count():
    """注入即记一次命中——用来分辨"真有用"和"从没被用过的噪声"。"""
    save_lesson("你就叫小月吧")
    save_lesson("回答简洁点")
    get_lessons_injection()
    get_lessons_injection()
    conn = connect()
    rows = conn.execute("SELECT content, hit_count FROM lessons").fetchall()
    conn.close()
    assert rows and all(r["hit_count"] == 2 for r in rows), \
        f"hit_count 未累计: {[(r['content'], r['hit_count']) for r in rows]}"


def test_identity_lesson_always_injected():
    """身份设定不占普通配额：塞满 10 条技术纠正后它仍必须在注入里。"""
    save_lesson("你就叫小月吧，记住了吗")
    for i in range(10):
        save_lesson(f"不对，技术细节第 {i} 条")
    text = get_lessons_injection(limit=5)
    assert "你就叫小月吧" in text, "身份设定被普通教训挤出了注入窗口"
    assert text.splitlines()[0].startswith("- 你就叫小月吧"), "identity 应排在最前"


def test_migration_dedupes_legacy_rows():
    """老库（无 UNIQUE 约束）里的副本行，迁移后按内容去重且保留最早时间。"""
    conn = connect()
    conn.execute("DROP TABLE lessons")
    conn.execute(
        "CREATE TABLE lessons (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "content TEXT NOT NULL, context TEXT DEFAULT '', created_at TEXT NOT NULL)"
    )
    for ts in ("2026-09-03T00:00:00+00:00", "2026-09-01T00:00:00+00:00",
               "2026-09-02T00:00:00+00:00"):
        conn.execute(
            "INSERT INTO lessons (content, context, created_at) VALUES ('测试教训', '', ?)",
            (ts,),
        )
    conn.execute(
        "INSERT INTO lessons (content, context, created_at) "
        "VALUES ('你就叫小月吧，记住了吗', '', '2026-08-26T07:22:37+00:00')"
    )
    conn.commit()
    conn.close()

    init_db()  # 触发 _migrate_lessons

    conn = connect()
    rows = conn.execute("SELECT content, created_at, kind FROM lessons").fetchall()
    conn.close()
    by_content = {r["content"]: r for r in rows}
    assert len(rows) == 2, f"未去重: {[dict(r) for r in rows]}"
    assert by_content["测试教训"]["created_at"] == "2026-09-01T00:00:00+00:00", \
        "应保留最早一条（首次被纠正的时间语义）"
    assert by_content["你就叫小月吧，记住了吗"]["kind"] == "identity"

    # 迁移幂等：再跑一次不炸、不变
    init_db()
    conn = connect()
    assert conn.execute("SELECT COUNT(*) AS c FROM lessons").fetchone()["c"] == 2
    conn.close()
