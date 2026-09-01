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


# ── 训练学知识卡与触发词覆盖（实测缺陷修复）──────────────────
# 动因：用户问"每个部位四个动作、一个动作四组、12 次"时命中 0 张卡——
# 15 张卡全是营养/生活方式，且触发词写的是"力量训练/抗阻"这类术语，
# 而用户真实说法是"练胸/卧推/四组"。她因此只能顺着用户确认，给不出判断。

@pytest.mark.parametrize("query", [
    "我要的是每个部位四个动作，一个动作四组",
    "练胸：平板卧推、上斜哑铃卧推、双杠臂屈伸、绳索夹胸",
    "练手臂：哑铃弯举、锤式弯举、绳索下压",
    "每个动作四组，次数还是按12次来",
    "练完胸隔天练手臂行吗",
    "我是新手，一周练几次合适",
    "深蹲硬拉怎么安排组数",
])
def test_real_user_phrasing_hits_cards(db, query):
    """用户的真实说法必须能命中训练学卡（这些原本全部命中 0 张）。"""
    fitness.seed_fitness_cards()
    assert fitness.get_fitness_facts(query), f"命中 0 张: {query}"


@pytest.mark.parametrize("query", [
    "今天天气不错", "帮我打开F盘", "现在几点了", "写一段小说",
])
def test_unrelated_query_no_cards(db, query):
    """无关话题不该命中——触发词扩太宽会污染每一次对话。"""
    fitness.seed_fitness_cards()
    assert fitness.get_fitness_facts(query) == [], f"误命中: {query}"


def test_training_science_cards_content(db):
    """训练学卡要给出可判断的量化依据，而不是泛泛而谈。"""
    fitness.seed_fitness_cards()
    volume = fitness.get_fitness_facts("一周练几次，每个部位几组")
    assert any("10~20" in h or "10~12" in h for h in volume), "缺每周容量区间"
    reps = fitness.get_fitness_facts("12次合适吗")
    assert any("6~12" in h for h in reps), "缺复合动作次数区间"
    recovery = fitness.get_fitness_facts("练完胸隔天练手臂")
    assert any("48~72" in h for h in recovery), "缺恢复窗口"
    assert any("肱三头" in h for h in recovery), "缺间接征用说明（本例的关键风险）"


def test_seed_widening_keywords_updates_not_duplicates(db):
    """扩充触发词时改索引、不插新行。

    原实现按 keywords 全串精确匹配做幂等——扩词等于换主键，会把同一张卡
    再插一份，查询时两张都命中、prompt 出现重复注入（线上第 15 张卡已因此
    与代码不一致）。现在按 content 判重。
    """
    fitness.seed_fitness_cards()
    conn = connect()
    total_before = conn.execute("SELECT COUNT(*) AS c FROM fitness_facts").fetchone()["c"]
    # 模拟老库：把某张卡的触发词改回旧的窄串
    conn.execute(
        "UPDATE fitness_facts SET keywords='力量训练,撸铁,抗阻,肌肉' "
        "WHERE content LIKE '减脂期间力量训练优先%'"
    )
    conn.commit()

    assert fitness.seed_fitness_cards() == 0, "扩词不应插入新行"

    conn = connect()
    assert conn.execute("SELECT COUNT(*) AS c FROM fitness_facts").fetchone()["c"] == total_before
    kw = conn.execute(
        "SELECT keywords FROM fitness_facts WHERE content LIKE '减脂期间力量训练优先%'"
    ).fetchone()["keywords"]
    assert "练胸" in kw, "触发词未被更新"


def test_no_duplicate_content_cards(db):
    """任何一条内容只应存在一行——重复内容会造成 prompt 重复注入。"""
    fitness.seed_fitness_cards()
    fitness.seed_fitness_cards()
    conn = connect()
    dupes = conn.execute(
        "SELECT content, COUNT(*) AS n FROM fitness_facts GROUP BY content HAVING n > 1"
    ).fetchall()
    assert not dupes, f"存在重复卡: {[d['content'][:30] for d in dupes]}"
