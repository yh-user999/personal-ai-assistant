"""被动目标追踪测试：意向识别 + 候选生命周期。

动因：goals 表长期 0 条，但用户原话里明明有目标（「我想在国庆之前减脂减重到
62.5KG以下」）——他只是从不打「目标：XXX」命令。同为命令式录入的
jargon_terms/writing_log 也全空，而被动识别的 concerns 有 22 条在用。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

from app.models.database import connect  # noqa: E402
from app.services import intent_goals as ig  # noqa: E402


# ── 意向识别：真目标（全部取自用户实际说过的话）────────────

@pytest.mark.parametrize("text,expect", [
    ("我想在国庆之前减脂减重到62.5KG以下，你帮我规划一下", "减脂减重"),
    ("我想写小说，我平时想到一些情节内容发给你", "写小说"),
    ("我想减脂，给个训练和饮食方案", "减脂"),
    ("接下来我要把测试工程和CI做完", "测试工程"),
    ("明天我打算练胸和有氧运动", "练胸"),
])
def test_detects_real_intents(text, expect):
    got = ig.detect_intents(text)
    assert got, f"漏判: {text}"
    assert any(expect in g for g in got), f"内容不对: {got}"


def test_short_intent_detected():
    """「我想减脂」只有 2 字——原来卡 4 字下限把最典型的表达全漏了。"""
    assert ig.detect_intents("我想减脂") == ["减脂"]


# ── 噪声排除（都是用户真说过的话）──────────────────────────

@pytest.mark.parametrize("text", [
    "又准备到午休时间了，真快啊",                    # 时间感慨
    "我是打算原身被打死，原身父亲无力反抗",            # 在讲剧情
    "外星人是偶然发现地球，打算研究",                 # 第三人称
    "但我想让原身被打死的冲突合理点",                 # 剧情设定
    "他打算明天去健身",                             # 不是自己
    "我想睡觉了",                                   # 生活动作
])
def test_rejects_noise(text):
    assert ig.detect_intents(text) == [], f"误判: {text}"


def test_novel_context_judged_on_intent_not_whole_message():
    """语境按抽出的意向判，不按整条消息。

    「我想写小说，我平时想到一些情节内容发给你」整句含"情节"，但意向本身
    （"写小说"）是干净的真目标——按整句判会误杀（实测就杀掉了）。
    """
    assert ig.detect_intents("我想写小说，我平时想到一些情节内容发给你") == ["写小说"]


# ── 候选生命周期 ──────────────────────────────────────────

def test_record_creates_candidate(db):
    ids = ig.record_intent("我想减脂")
    assert len(ids) == 1
    goals = ig.list_goals()
    assert goals[0]["status"] == ig.STATUS_CANDIDATE
    assert goals[0]["source"] == ig.SOURCE_PASSIVE


def test_record_dedupes(db):
    ig.record_intent("我想在国庆之前减脂减重到62.5KG以下")
    ig.record_intent("我想在国庆之前减脂减重到62.5KG以下，再说一遍")
    assert len(ig.list_goals()) == 1, "同一目标不该重复建"


def test_promote_and_drop(db):
    gid = ig.record_intent("我想减脂")[0]
    assert ig.promote(gid) is True
    assert ig.list_goals()[0]["status"] == ig.STATUS_ACTIVE
    assert ig.promote(gid) is False, "已转正的不该再被 promote"
    assert ig.drop(gid) is True
    assert ig.list_goals()[0]["status"] == ig.STATUS_DROPPED


def test_followup_picks_candidate(db):
    ig.record_intent("我想把CI做完")
    item = ig.pick_followup()
    assert item and "CI" in item["title"]


def test_followup_respects_interval(db):
    """追问要有间隔——刚问过不该立刻再问。"""
    ig.record_intent("我想减脂")
    assert ig.build_injection(), "首次应能追问"
    assert ig.build_injection() == "", "刚问过不该立刻再问"


def test_dropped_after_max_asks(db):
    """问过两次没回应就丢弃——问两遍就从关心变催促。"""
    gid = ig.record_intent("我想减脂")[0]
    for _ in range(ig.MAX_ASK):
        ig.mark_asked(gid)
    assert ig.pick_followup() is None
    assert ig.list_goals()[0]["status"] == ig.STATUS_DROPPED


def test_injection_tells_not_to_force(db):
    """提示里要明确"接不上就别硬提"——这类跟进最怕变成打扰。"""
    ig.record_intent("我想减脂")
    out = ig.build_injection()
    assert "别硬提" in out or "接得上" in out
    assert "重复问" in out


def test_no_injection_without_candidates(db):
    assert ig.build_injection() == ""


def test_user_isolation(db):
    ig.record_intent("我想减脂", user_id="owner")
    assert ig.list_goals(user_id="123456") == []
    assert len(ig.list_goals(user_id="owner")) == 1
