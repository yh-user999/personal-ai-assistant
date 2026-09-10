"""新闻/时事获取：意图判定与深挖并发的回归测试。

这些用例全部来自生产 trace 里**真实发生过的漏检索**，不是想象的边界：

- "特朗普最近有什么动作""日本核污水排海最新情况""现在黄金多少钱一克"
  有主体、有时效诉求，但用词都不在新闻名词表里（名词表只有"新闻/事件/
  事故"这类），于是判定为 False、直接跳过联网，模型只能凭旧记忆作答——
  这正是代码注释里记录过的那类生产事故。
- "现在呢"发生在新闻讨论中间，追问的是新进展，却因为自身没有主体被判为
  普通闲聊，把上一轮的报道复述一遍当作回答。

判定是"证据是否足够"的闸门：漏判的代价是拿训练记忆冒充时事。
反向的误判同样有害（闲聊被迫联网，并因"无来源只能说未查到"而答非所问），
所以下面同时锁住不许联网的一侧。
"""
from __future__ import annotations

import asyncio

import pytest

from app.chat import response_plan, web_provider


# ── 通道 2：时效词 + 求信息 + 外部主体 ──────────────────────

@pytest.mark.parametrize("text", [
    "特朗普最近有什么动作",
    "日本核污水排海最新情况",
    "中美关系现在怎么样了",
    "现在黄金多少钱一克",
    "台风到哪了",
    "最近物价怎么样",
])
def test_timeliness_question_with_external_subject_triggers_search(text):
    """有时效诉求、有外部主体，即使不含"新闻/事件"字样也要联网。"""
    assert web_provider.needs_web_search(text) is True


@pytest.mark.parametrize("text", [
    "最近的进展不错",
    "最近的项目进展",
    "最近的代码质量",
    "今天要做什么",
    "今天天气如何",
    "最近学习怎么样",
    "最近 API 有什么变化",
    "最近不顺利吗",
    "帮我写个函数",
])
def test_self_or_daily_topics_do_not_trigger_search(text):
    """自指/日常事务不许被拖去联网。

    这些句子同样含时效词和疑问，差别只在于谈的是用户自己或助手的日常，
    没有外部主体。误判会让闲聊被迫联网，还会因"无来源只能说未查到"而
    答非所问。
    """
    assert web_provider.needs_web_search(text) is False


def test_existing_news_noun_channel_still_works():
    """原有通道不能被新通道挤掉。"""
    assert web_provider.needs_web_search("最近有什么新闻") is True
    assert web_provider.needs_web_search("湖南那个事件最新进展") is True


# ── 指代式追问继承上一轮检索需求 ────────────────────────────

_NEWS_TURN = [
    {"role": "user", "content": "特朗普最近有什么动作"},
    {"role": "assistant", "content": "我看到几条报道……"},
]
_CASUAL_TURN = [
    {"role": "user", "content": "帮我写个函数"},
    {"role": "assistant", "content": "好的……"},
]


def test_followup_inherits_search_from_news_turn():
    """新闻是会变的事实：刚看完成果问"现在呢"，要的是新进展。"""
    plan = response_plan.build_rule_plan("现在呢", is_owner=True, history=_NEWS_TURN)
    assert plan.provider == "web_search"
    assert plan.retrieval_required is True
    assert plan.tool_required is True


def test_followup_uses_previous_topic_as_query():
    """追问本身当检索词毫无价值（"现在呢"），要用上一轮的话题词。"""
    plan = response_plan.build_rule_plan("现在呢", is_owner=True, history=_NEWS_TURN)
    assert plan.query == "特朗普最近有什么动作"


def test_followup_does_not_inherit_from_casual_turn():
    """普通闲聊的追问不该被拖去联网。"""
    plan = response_plan.build_rule_plan("现在呢", is_owner=True, history=_CASUAL_TURN)
    assert plan.provider != "web_search"


@pytest.mark.parametrize("text", ["继续写第三章", "帮我把这个改一下"])
def test_creative_and_action_requests_are_not_followups(text):
    """创作/动作指令不能被追问规则吃掉。"""
    assert web_provider.looks_like_followup(text) is False


def test_followup_without_history_is_ignored():
    """没有历史时不能凭空继承。"""
    assert web_provider.needs_web_search_after_followup("现在呢", "") is False


# ── planner 提示词必须写明 web_search ──────────────────────

def test_planner_prompt_documents_web_search_provider():
    """planner 是主判，但它原先只被告知 current_datetime/calculator。

    生产 trace 里出现过 planner 把 intent 正确识别成"查找更多新闻来源"、
    却把 provider 留空的情况——它根本不知道有 web_search 这个取值。
    """
    messages = response_plan.build_planner_messages(
        "你再看看有没有其他新闻来源", [], response_plan.build_rule_plan("x")
    )
    system = messages[0]["content"]
    assert "web_search" in system
    assert "hotboard" in system


# ── 深挖多角度查询必须并发 ──────────────────────────────────

def test_deep_dive_angle_queries_run_concurrently(monkeypatch):
    """角度查询彼此正交、互不依赖，串行排队会白白拉长关键路径。

    实测串行时深挖要 6.6s；用"同时在飞的最大请求数"断言，避免依赖墙钟
    时间造成用例不稳定。
    """
    inflight = 0
    peak = 0
    seen: list[str] = []

    async def fake_search(query, *, category="general", time_range="week", limit=10):
        nonlocal inflight, peak
        seen.append(query)
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.05)
        inflight -= 1
        return []

    monkeypatch.setattr(web_provider, "web_search", fake_search)
    asyncio.run(web_provider.search_and_cluster("某某事件", deep_dive=True))

    # 角度查询带空格（"某某事件 监控 经过 完整"），主查询与放宽尝试都是裸词。
    angles = [q for q in seen if " " in q]
    assert len(angles) >= 2, f"未发起角度查询: {seen}"
    assert peak >= 2, "角度查询仍在串行执行"


def test_deep_dive_survives_one_angle_failing(monkeypatch):
    """单个角度失败不能牵连主结果——深挖只是加料。"""
    async def fake_search(query, *, category="general", time_range="week", limit=10):
        if query.endswith("完整"):
            raise RuntimeError("boom")
        return [{
            "title": query[:10], "url": f"https://x.com/{abs(hash(query))}",
            "source": "媒体", "published_at": "2026-09-10T00:00:00+00:00",
            "time_known": True,
        }]

    monkeypatch.setattr(web_provider, "web_search", fake_search)
    data = asyncio.run(web_provider.search_and_cluster("某某事件", deep_dive=True))
    assert data["has_sources"] is True
    assert data["angle_added"] >= 1
