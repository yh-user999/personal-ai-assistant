"""主观时间测试：锚点提炼、相对日、退回原始日期。"""
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

from app.models.database import connect
from app.services import subjective_time as st

TODAY = date(2026, 9, 1)


def _seed_summary(d: str, content: str) -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO daily_summaries (date, content, created_at) VALUES (?, ?, ?)",
        (d, content, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _seed_worklog(d: str, content: str) -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO work_log (date, content, created_at) VALUES (?, ?, ?)",
        (d, content, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


# ── 锚点提炼：宁可放弃，也不要截在半个词上 ──────────────────

@pytest.mark.parametrize("raw,expected", [
    # work_log 常带时间前缀，留着会变成"下午3点到5点完成了X那阵子"
    ("下午3点到5点完成了记忆系统实验", "完成了记忆系统实验"),
    ("下午2-4点调参", "调参"),
    # 小结的 Markdown 与套话开头都要剥掉
    ("**深度编码与资料浏览并行的一天**", "深度编码与资料浏览并行的一天"),
    ("**咖啡与浏览器主导的一天** - 购买了一杯超大杯生椰拿铁", "咖啡与浏览器主导的一天"),
    ("今日主要围绕接码平台记录、玄幻架空明代小说设定创作及日常闲聊展开。", "接码平台记录"),
])
def test_clean_title(raw, expected):
    assert st._clean_title(raw) == expected


def test_clean_title_gives_up_on_unsplittable_long_text():
    """长且无句读可切时返回空串——不能硬截出"玄幻架空明"这种半个词。"""
    assert st._clean_title("一段没有任何标点符号的超长文本内容持续不断地写下去直到很长") == ""


def test_clean_title_empty():
    assert st._clean_title("") == ""
    assert st._clean_title("   ") == ""


# ── 相对日优先 ────────────────────────────────────────────

@pytest.mark.parametrize("day,expected", [
    ("2026-09-01", "今天"),
    ("2026-08-31", "昨天"),
    ("2026-08-30", "前天"),
])
def test_relative_days(db, day, expected):
    assert st.describe(f"{day}T10:00:00+00:00", anchors={}, today=TODAY) == expected


def test_anchor_used_beyond_three_days(db):
    _seed_summary("2026-08-28", "**接码平台记录**")
    text = st.describe("2026-08-28T10:00:00+00:00", today=TODAY)
    assert "接码平台记录" in text and text.endswith("那阵子")


def test_no_anchor_returns_empty(db):
    """没锚点时返回空串，由调用方退回原始日期。"""
    assert st.describe("2026-08-20T10:00:00+00:00", anchors={}, today=TODAY) == ""


def test_worklog_overrides_summary(db):
    """同一天 work_log 优先——用户亲手记的更贴近他自己的记忆。"""
    _seed_summary("2026-08-25", "**自动生成的小结标题**")
    _seed_worklog("2026-08-25", "下午3点到5点完成了记忆系统实验")
    assert st.get_anchors()["2026-08-25"] == "完成了记忆系统实验"


def test_title_phrase_strips_suffix():
    assert st.title_phrase("深度编码与资料浏览并行的一天") == "深度编码与资料浏览并行"
    assert st.title_phrase("调参") == "调参"


# ── 注入格式 ──────────────────────────────────────────────

def test_format_injection_uses_anchor(db):
    _seed_summary("2026-08-28", "**接码平台记录**")
    mems = [{"id": 1, "ts": "2026-08-28T10:00:00+00:00", "content": "聊了接码的事"}]
    out = st.format_injection(mems)
    assert "[记忆]" in out
    assert "2026-08-28" not in out, "有锚点时不该再出现原始日期"


def test_format_injection_falls_back_to_date(db):
    """无锚点可用时保留原始日期，不能丢失时间信息。"""
    mems = [{"id": 1, "ts": "2026-07-01T10:00:00+00:00", "content": "很久以前的事"}]
    assert "2026-07-01" in st.format_injection(mems)


def test_format_injection_empty(db):
    assert st.format_injection([]) == ""


def test_format_injection_uses_summary_when_present(db):
    # 用"今天"的真实日期构造时间戳（旧实现硬编码 2026-09-01，跨天即炸）
    from zoneinfo import ZoneInfo

    today_str = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    mems = [{"id": 1, "ts": f"{today_str}T12:00:00+08:00",
             "content": "原文很长" * 20, "summary": "摘要"}]
    out = st.format_injection(mems)
    assert "摘要" in out and "今天" in out


def test_bad_timestamp_is_tolerated(db):
    assert st.describe("not-a-date", anchors={}, today=TODAY) == ""
    assert st.describe("", anchors={}, today=TODAY) == ""
