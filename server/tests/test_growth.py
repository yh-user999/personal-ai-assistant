"""成长感知测试：自我否定信号识别 + 事实证据聚合。

动因（用户原话，在 369 条消息里反复出现）：
「我也没做什么，都是交给ai做，自己感觉没什么收获」
「今天没什么心思工作和学习，怎么办」
现有模块一个都答不上——只能泛泛安慰，而 3369 条行为事件的数据一直没被用。
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

from app.models.database import connect  # noqa: E402
from app.services import growth  # noqa: E402


def _iso(days_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _seed_commit(days_ago: float = 1) -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO behavior_events (kind, name, start_ts) VALUES ('git_commit','repo',?)",
        (_iso(days_ago),),
    )
    conn.commit()


def _seed_app(name: str, hours: float, days_ago: float = 1) -> None:
    start = datetime.now(timezone.utc) - timedelta(days=days_ago)
    conn = connect()
    conn.execute(
        "INSERT INTO behavior_events (kind, name, start_ts, end_ts) VALUES ('app_usage',?,?,?)",
        (name, start.isoformat(), (start + timedelta(hours=hours)).isoformat()),
    )
    conn.commit()


# ── 信号识别 ──────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "我也没做什么，都是交给ai做，自己感觉没什么收获",
    "今天没什么心思工作和学习，怎么办",
    "我每天工作学习耐心都不足，能怎么办",
    "感觉最近没进步",
    "有点迷茫",
    "提不起劲",
    "感觉在浪费时间",
    "都是AI做的",
])
def test_detects_self_doubt(text):
    """中文口语常在词间插字——「没什么收获」「耐心都不足」「没什么心思」
    写死短语全会漏，所以判据用正则。"""
    assert growth.detect_self_doubt(text), f"漏判: {text}"


@pytest.mark.parametrize("text", [
    "我减脂两周后平台期了不掉秤怎么办",   # "怎么办"太宽，刻意不列为信号
    "这个报错怎么办",
    "有哪些命丛",
    "今天天气不错",
    "帮我打开F盘",
    "我没做过这个动作",
])
def test_no_false_positives(text):
    assert not growth.detect_self_doubt(text), f"误判: {text}"


# ── 证据聚合 ──────────────────────────────────────────────

def test_collect_commits(db):
    for _ in range(5):
        _seed_commit(days_ago=2)
    assert growth.collect_evidence(days=7)["commits"] == 5


def test_excludes_out_of_range(db):
    _seed_commit(days_ago=30)
    assert growth.collect_evidence(days=7)["commits"] == 0


def test_separates_work_and_leisure(db):
    """娱乐类应用单列，不混进工作时长——否则"产出"数字虚高。"""
    _seed_app("pythonw.exe", 5.0)
    _seed_app("dota2.exe", 3.0)
    ev = growth.collect_evidence(days=7)
    assert ev["work_hours"] == pytest.approx(5.0, abs=0.2)
    assert ev["leisure_hours"] == pytest.approx(3.0, abs=0.2)


def test_topics_from_consolidation(db):
    conn = connect()
    conn.execute(
        "INSERT INTO memories (user_id, sender, content, topics, ts) "
        "VALUES ('owner','user','x','[\"RAG调优\",\"小说创作\"]',?)", (_iso(1),),
    )
    conn.commit()
    ev = growth.collect_evidence(days=7)
    assert ev["topic_count"] == 2
    assert "RAG调优" in ev["topics"]


def test_worklog_deduplicated(db):
    """work_log 有重复行（测试曾灌入 18 条同样的"下午2-4点调参"），
    不去重会让注入里出现三遍同一句手记。"""
    today = datetime.now(timezone.utc).date().isoformat()
    conn = connect()
    for _ in range(5):
        conn.execute(
            "INSERT INTO work_log (date, content, created_at) VALUES (?,'调参',?)",
            (today, _iso()),
        )
    conn.commit()
    logs = growth.collect_evidence(days=7)["work_logs"]
    assert len(logs) == 1, f"未去重: {logs}"


def test_counts_corrections(db):
    """被纠正次数是判断力的体现——他在校准她，这也算收获。"""
    conn = connect()
    conn.execute(
        "INSERT INTO lessons (content, context, created_at, kind) "
        "VALUES ('纠正一句','', ?, 'style')", (_iso(1),),
    )
    conn.commit()
    assert growth.collect_evidence(days=7)["corrections"] == 1


# ── 注入 ──────────────────────────────────────────────────

def test_no_injection_without_evidence(db):
    """没证据就别硬凑——宁可不注入也不空泛安慰。"""
    assert growth.build_injection() == ""


def test_injection_has_concrete_numbers(db):
    for _ in range(12):
        _seed_commit(days_ago=1)
    _seed_app("pythonw.exe", 6.0)
    out = growth.build_injection()
    assert "12 次" in out, "缺具体提交数"
    assert "6.0h" in out or "6h" in out, "缺时长"


def test_injection_forbids_empty_comfort(db):
    """注入里必须明确"不要空泛安慰"——这是这个模块的核心取向。"""
    _seed_commit(days_ago=1)
    out = growth.build_injection()
    assert "不要空泛安慰" in out
    assert "你已经很努力了" in out, "应明确禁止这类话"
    assert "判断与决策" in out, "应指出他忽略的那部分收获"


def test_leisure_mentioned_only_when_significant(db):
    _seed_commit(days_ago=1)
    _seed_app("dota2.exe", 0.5)
    assert "娱乐类" not in growth.build_injection()
    _seed_app("dota2.exe", 4.0)
    assert "娱乐类" in growth.build_injection()
