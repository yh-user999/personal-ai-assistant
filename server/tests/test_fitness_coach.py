"""AI 健身计划草案校验测试。"""
import json

import pytest

from app.services import fitness_catalog, fitness_coach


@pytest.mark.asyncio
async def test_generate_plan_returns_validated_draft_without_persisting(db, monkeypatch):
    exercise = fitness_catalog.list_exercises(query="卧推", limit=10)[0]
    captured = {}

    async def fake_chat(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return json.dumps(
            {
                "name": "AI 全身计划",
                "goal": "减脂保肌",
                "days": [
                    {
                        "day_index": 1,
                        "name": "全身 A",
                        "exercises": [
                            {
                                "exercise_id": exercise["id"],
                                "sets": 3,
                                "rep_min": 8,
                                "rep_max": 12,
                                "rir_target": 2,
                                "rest_seconds": 120,
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        )

    from app.services import fitness_coach as coach
    monkeypatch.setattr(coach.llm, "chat", fake_chat)
    result = await coach.generate_plan_draft(
        "owner",
        {"goal": "减脂保肌", "sessions_per_week": 3, "session_minutes": 45},
        request_id="fitness-test",
    )
    assert result["draft"]["source"] == "ai"
    assert result["draft"]["days"][0]["exercises"][0]["exercise_id"] == exercise["id"]
    assert captured["kwargs"]["response_format"] == {"type": "json_object"}

    from app.services import fitness_training
    assert fitness_training.list_plans("owner") == []


@pytest.mark.asyncio
async def test_invalid_ai_action_is_rejected(db, monkeypatch):
    async def fake_chat(messages, **kwargs):
        return '{"name":"坏计划","days":[{"name":"A","exercises":[{"exercise_id":999999,"sets":3,"rep_min":8,"rep_max":12}]}]}'

    monkeypatch.setattr(fitness_coach.llm, "chat", fake_chat)
    with pytest.raises(ValueError, match="候选列表之外"):
        await fitness_coach.generate_plan_draft("owner", {"goal": "增肌"})
