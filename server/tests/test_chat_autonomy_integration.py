"""自主 planner 与回复反思的「开启后确实生效」集成测试。

单元测试只覆盖各自的解析与判定；这里走完整 /api/chat 链路，验证：
- planner 全量生效时，LLM 给出的计划真的改写了 system prompt
- shadow 模式下计划只记录、不改变行为
- 反思触发时会调用审校，并在需要时用重写结果替换候选回复
- 审校失败时保留候选回复，不影响主链路
"""
import pytest

from app.config import settings
from app.models.database import init_db, reset_connections


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "api_token", "")
    reset_connections()
    init_db()
    yield
    reset_connections()


def _install_llm(monkeypatch, *, planner_reply=None, review_reply=None, revise_reply="修订后的回复"):
    """安装分用途的 LLM 桩，返回捕获容器。"""
    box = {"reply_prompts": [], "purpose": []}

    async def fake_chat(messages, **kwargs):
        purpose = kwargs.get("purpose", "")
        box["purpose"].append(purpose)
        if purpose == "planner":
            return planner_reply if planner_reply is not None else '{"mode": "casual_chat", "confidence": 0.9}'
        if purpose == "review":
            return review_reply if review_reply is not None else (
                '{"needs_revision": false, "scores": {"relevance": 1.0, "state_fit": 1.0, "grounding": 1.0,'
                ' "tone": 1.0, "brevity": 1.0, "safety": 1.0, "stance_clarity": 1.0,'
                ' "noise_resistance": 1.0, "proportionality": 1.0}, "issues": [], "revision_plan": []}'
            )
        if purpose == "revise":
            return revise_reply
        box["reply_prompts"].append(messages[0]["content"])
        return "候选回复"

    import app.api.chat as _chat_api
    import app.core.embedding as _embedding

    monkeypatch.setattr(_chat_api.llm, "chat", fake_chat)

    async def fake_embed(texts):
        return [[0.0] * settings.embedding_dimension for _ in texts]

    monkeypatch.setattr(_embedding, "embed", fake_embed)
    return box


def _post(message):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        return client.post("/api/chat", json={"message": message}).json()


# ── planner 生效与 shadow ───────────────────────────────────

def test_planner_plan_reaches_system_prompt(env, monkeypatch):
    monkeypatch.setattr(settings, "semantic_planner_enabled", True)
    monkeypatch.setattr(settings, "semantic_planner_shadow_only", False)
    box = _install_llm(
        monkeypatch,
        planner_reply='{"mode": "reasoning", "intent": "analysis", "confidence": 0.92,'
                      ' "constraints": ["必须给出判断依据XY"]}',
    )

    _post("这个方案靠谱吗")

    assert "reasoning" in box["reply_prompts"][0]
    assert "必须给出判断依据XY" in box["reply_prompts"][0]


def test_shadow_mode_does_not_change_prompt(env, monkeypatch):
    """shadow 模式：计划被记录，但不采用，prompt 仍走规则计划。"""
    monkeypatch.setattr(settings, "semantic_planner_enabled", True)
    monkeypatch.setattr(settings, "semantic_planner_shadow_only", True)
    box = _install_llm(
        monkeypatch,
        planner_reply='{"mode": "reasoning", "intent": "analysis", "confidence": 0.92,'
                      ' "constraints": ["必须给出判断依据XY"]}',
    )

    _post("这个方案靠谱吗")

    assert "必须给出判断依据XY" not in box["reply_prompts"][0]


def test_planner_disabled_skips_planner_call(env, monkeypatch):
    monkeypatch.setattr(settings, "semantic_planner_enabled", False)
    box = _install_llm(monkeypatch)

    _post("这个方案靠谱吗")

    assert "planner" not in box["purpose"]


# ── 反思生效 ────────────────────────────────────────────────

def test_reflection_revision_replaces_reply(env, monkeypatch):
    monkeypatch.setattr(settings, "reflection_enabled", True)
    box = _install_llm(
        monkeypatch,
        review_reply='{"needs_revision": true, "scores": {"safety": 1.0, "relevance": 0.4},'
                     ' "issues": ["答非所问"], "revision_plan": ["重写"]}',
        revise_reply="修订后的回复",
    )

    result = _post("李羽的能力是什么")

    assert result["reply"] == "修订后的回复"
    assert "review" in box["purpose"] and "revise" in box["purpose"]


def test_reflection_passes_through_when_no_revision_needed(env, monkeypatch):
    monkeypatch.setattr(settings, "reflection_enabled", True)
    box = _install_llm(monkeypatch)

    result = _post("李羽的能力是什么")

    assert result["reply"] == "候选回复"
    assert "revise" not in box["purpose"]


def test_review_failure_keeps_draft(env, monkeypatch):
    """审校返回非法 JSON → 保留候选回复，不因审校问题改坏主链路。"""
    monkeypatch.setattr(settings, "reflection_enabled", True)
    box = _install_llm(monkeypatch, review_reply="这不是 JSON")

    result = _post("李羽的能力是什么")

    assert result["reply"] == "候选回复"
    assert "revise" not in box["purpose"]


def test_reflection_disabled_skips_review(env, monkeypatch):
    monkeypatch.setattr(settings, "reflection_enabled", False)
    box = _install_llm(monkeypatch)

    _post("李羽的能力是什么")

    assert "review" not in box["purpose"]


def test_simple_greeting_skips_reflection_even_when_enabled(env, monkeypatch):
    """简单寒暄不额外烧一次审校调用。"""
    monkeypatch.setattr(settings, "reflection_enabled", True)
    box = _install_llm(monkeypatch)

    _post("你好")

    assert "review" not in box["purpose"]
