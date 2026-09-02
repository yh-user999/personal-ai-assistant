"""路由评测基准（检索可观测性 P0）：量化"意图路由准不准"。

种子数据复刻生产形态（两本小说 + 实体/设定卡/人物 + 项目/手册/简历文档 + facts），
对 fixtures/route_benchmark.json 逐条跑真实路由函数（detect_domains /
detect_enum_intent / 自愈诊断），统计准确率并设阈值断言——规则改坏立刻变红。

注意：fixture 期望值与路由规则互为镜像。若规则有意变更（如新增体系词），
应同步更新 fixture，而不是为了过测试改期望。
"""
import json
from pathlib import Path

import pytest

from app.config import settings
from app.core import knowledge
from app.models.database import connect, init_db, reset_connections
from app.services import index_healer
from app.services.knowledge_domain import detect_domains

FIXTURE = Path(__file__).parent / "fixtures" / "route_benchmark.json"

# 阈值：域路由与枚举识别是确定性规则，低于 95% 说明 fixture 或规则有回归
MIN_ACCURACY = 0.95


@pytest.fixture(scope="module")
def seeded_env(tmp_path_factory):
    db_file = tmp_path_factory.mktemp("bench") / "bench.db"
    _orig_db_path = settings.db_path
    settings.db_path = str(db_file)  # 直接改单例（module 级 fixture 不可用 monkeypatch）
    reset_connections()
    init_db()
    conn = connect()

    def chunk(doc, domain, idx, content):
        cur = conn.execute(
            "INSERT INTO knowledge_chunks (doc_name, chunk_index, content, created_at) "
            "VALUES (?, ?, ?, '2026-09-01T00:00:00+00:00')",
            (doc, idx, content),
        )
        conn.execute(
            "UPDATE knowledge_chunks SET domain=? WHERE id=?", (domain, cur.lastrowid)
        )
        conn.execute(
            "INSERT INTO knowledge_fts (chunk_id, grams) VALUES (?, ?)",
            (cur.lastrowid, knowledge._grams_text(content)),
        )
        return cur.lastrowid

    # 两本小说（寂静杀戮：命丛/功法/势力内容；食物链：另一套）
    chunk("小说-寂静杀戮", "novel", 1, "命丛是道术的根基，夜海就是其中一个命丛。")
    chunk("小说-寂静杀戮", "novel", 2, "银河灵潮是左志诚觉醒的第一个命丛。")
    chunk("小说-寂静杀戮", "novel", 3, "命图共有四种，是命丛的组合图景。")
    chunk("小说-寂静杀戮", "novel", 4, "蜃宗是南圣门的一支势力，隐居多年。")
    chunk("小说-寂静杀戮", "novel", 5, "北鹏垂天式是南圣门真传功法。")
    chunk("小说-寂静杀戮", "novel", 6, "左志诚就是左擎苍，换了名字而已。")
    chunk("小说-食物链顶端的男人", "novel", 7, "血凰是林哲觉醒的异能之一。")
    chunk("小说-食物链顶端的男人", "novel", 8, "主角在食物链中不断进化。")
    # 项目/手册/简历文档
    chunk("小白零基础反代教程", "manual", 9, "反向代理就是把请求转发到后端服务。")
    chunk("OPS", "project_doc", 10, "执行器白名单在 .env 的 EXECUTOR_ALLOWED_ROOTS 配置。")
    chunk("简历（脱敏版）", "resume", 11, "求职简历的写法与模板。")

    # 实体索引（160 个的缩影：三类各两个）
    for book, name, kind in [
        ("小说-寂静杀戮", "夜海", "命丛"),
        ("小说-寂静杀戮", "银河灵潮", "命丛"),
        ("小说-寂静杀戮", "蜃宗", "势力"),
        ("小说-寂静杀戮", "南圣门", "势力"),
        ("小说-寂静杀戮", "北鹏垂天式", "功法"),
        ("小说-食物链顶端的男人", "血凰", "命丛"),
    ]:
        conn.execute(
            "INSERT INTO novel_entities (book, name, kind, first_chunk, note, created_at) "
            "VALUES (?, ?, ?, 0, '', '2026-09-01T00:00:00+00:00')",
            (book, name, kind),
        )

    # 设定卡（人物名归属 + 李羽）
    conn.execute(
        "INSERT INTO novel_facts (book, keywords, content, created_at) "
        "VALUES ('小说-寂静杀戮', '左志诚,左擎苍,李羽', '人物设定', '2026-09-01T00:00:00+00:00')"
    )
    # facts（王动 → 知识库几乎无内容 → __skip__ 哨兵）
    conn.execute(
        "INSERT INTO facts (user_id, subject, predicate, object, updated_at) "
        "VALUES ('owner', '王动', '身份', '路人', '2026-09-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    yield
    settings.db_path = _orig_db_path
    reset_connections()


def _load_cases():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def test_route_benchmark_accuracy(seeded_env):
    cases = _load_cases()
    assert len(cases) >= 25, "评测集规模不应缩水"

    failures = []
    for c in cases:
        q = c["query"]
        domains, docs = detect_domains(q)
        # 域路由比对（__skip__ 哨兵对 fixtures 同样适用）
        dom_ok = domains == c["domains"] and docs == c["docs"]
        enum_ok = index_healer.detect_enum_intent(q) == c["enum"]
        # 自愈诊断期望：fixture 用 heal 字段表示"应触发"（在空命中块下诊断）
        diag = index_healer.diagnose(q, domains, docs, [])
        heal_ok = (diag is not None) == c["heal"]
        if not (dom_ok and enum_ok and heal_ok):
            failures.append(
                f"「{q}」 期望 域={c['domains']}/{c['docs']} enum={c['enum']} heal={c['heal']}，"
                f"实际 域={domains}/{docs} enum={index_healer.detect_enum_intent(q)} "
                f"heal={diag is not None}"
            )

    total = len(cases)
    ok = total - len(failures)
    acc = ok / total
    if failures:
        print("\n".join(failures))
    assert acc >= MIN_ACCURACY, (
        f"路由评测准确率 {acc:.1%} 低于阈值 {MIN_ACCURACY:.0%}，失败 {len(failures)} 条"
    )
