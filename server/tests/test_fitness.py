"""第 6.29 课测试：健身台账 + 健身知识卡。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.models.database import connect, init_db  # noqa: E402
from app.services import fitness  # noqa: E402


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    init_db()
    yield


# ── 解析 ───────────────────────────────────────────────────

def test_parse_weight():
    assert fitness.parse_weight("记录体重：70.5") == 70.5
    assert fitness.parse_weight("体重70") == 70.0
    assert fitness.parse_weight("体重 68.2 公斤") == 68.2
    assert fitness.parse_weight("记录体重：abc") is None
    assert fitness.parse_weight("记录体重：5") is None  # 范围外


def test_parse_training():
    assert fitness.parse_training("训练记录：深蹲5x5 卧推60kg") == "深蹲5x5 卧推60kg"
    assert fitness.parse_training("健身记录：跑了40分钟") == "跑了40分钟"
    assert fitness.parse_training("训练记录：") is None
    assert fitness.parse_training("今天吃什么") is None


# ── 台账 ───────────────────────────────────────────────────

def test_summary_weight_trend(db):
    fitness.add_log("weight", 70.0, "")
    fitness.add_log("weight", 69.4, "")
    s = fitness.fitness_summary()
    assert "当前体重：69.4 kg" in s
    assert "起始 70.0 kg" in s
    assert "变化 -0.6 kg" in s


def test_summary_training_and_warning(db):
    fitness.add_log("weight", 70.0, "")
    s = fitness.fitness_summary()
    assert "⚠️ 训练记录：还没有" in s


def test_summary_empty(db):
    assert "还没有健身记录" in fitness.fitness_summary()


# ── 知识卡 ─────────────────────────────────────────────────

def test_seed_cards_idempotent(db):
    n1 = fitness.seed_fitness_cards()
    assert n1 >= 12
    n2 = fitness.seed_fitness_cards()
    assert n2 == 0  # 幂等


def test_get_fitness_facts_keyword(db):
    fitness.seed_fitness_cards()
    hits = fitness.get_fitness_facts("减脂遇到平台期怎么办")
    assert hits
    assert any("平台期" in h for h in hits)
    assert "出处" in hits[0] or "2024" in hits[0] or "2025" in hits[0]


def test_get_fitness_facts_no_match(db):
    fitness.seed_fitness_cards()
    assert fitness.get_fitness_facts("今天天气不错") == []


def test_progress_words_not_training():
    """「健身记录查询」是查进度，不能被解析成训练记录。"""
    assert fitness.parse_training("健身记录查询") is None
    assert "健身记录查询" in fitness.PROGRESS_WORDS
