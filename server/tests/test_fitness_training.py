"""结构化训练计划、会话和统计测试。"""
import pytest

from app.services import fitness_catalog, fitness_training


def _exercise(name: str) -> dict:
    return next(item for item in fitness_catalog.list_exercises(query=name, limit=20) if item["name"] == name)


def _plan(exercise_id: int, *, name: str = "三练计划") -> dict:
    return {
        "name": name,
        "goal": "减脂保肌",
        "days": [
            {
                "day_index": 1,
                "name": "全身 A",
                "exercises": [
                    {
                        "exercise_id": exercise_id,
                        "sets": 3,
                        "rep_min": 8,
                        "rep_max": 12,
                        "rir_target": 2,
                    }
                ],
            }
        ],
    }


def test_plan_session_set_and_summary(db):
    fitness_catalog.seed_builtin_exercises()
    bench = _exercise("卧推")
    plan = fitness_training.create_plan("owner", _plan(bench["id"]))
    assert plan["status"] == "draft"
    assert plan["days"][0]["exercises"][0]["name"] == "卧推"

    active = fitness_training.activate_plan("owner", plan["id"])
    assert active["status"] == "active"
    assert fitness_training.get_active_plan("owner")["id"] == plan["id"]

    session = fitness_training.start_session(
        "owner",
        plan_id=plan["id"],
        plan_day_id=plan["days"][0]["id"],
    )
    logged = fitness_training.log_set(
        "owner",
        session["id"],
        bench["id"],
        reps=8,
        weight_kg=60,
    )
    assert logged["exercise_name"] == "卧推"
    completed = fitness_training.complete_session("owner", session["id"])
    assert completed["status"] == "completed"
    assert completed["set_count"] == 1
    assert completed["volume_kg"] == 480

    summary = fitness_training.get_summary("owner", days=30)
    assert summary["sessions"]["completed"] == 1
    assert summary["volume_kg"] == 480
    assert summary["recent_prs"][0]["name"] == "卧推"
    assert summary["recent_prs"][0]["estimated_1rm"] == 76.0


def test_measurement_mirrors_legacy_weight_and_isolated(db):
    measurement = fitness_training.record_measurement("10001", kind="weight", value=70.5)
    assert measurement["unit"] == "kg"
    owner_summary = fitness_training.get_summary("10001")
    assert owner_summary["weight_trend"]["latest"] == 70.5

    other_summary = fitness_training.get_summary("10002")
    assert other_summary["weight_trend"] is None


def test_external_session_id_is_idempotent(db):
    first = fitness_training.start_session("owner", source="hevy", external_id="workout-1")
    second = fitness_training.start_session("owner", source="hevy", external_id="workout-1")
    assert second["id"] == first["id"]
    assert len(fitness_training.list_sessions("owner")) == 1


def test_plan_validation_rejects_unknown_action(db):
    with pytest.raises(ValueError, match="动作不存在"):
        fitness_training.create_plan("owner", _plan(99999))


def test_plan_validation_rejects_duplicate_action_order(db):
    fitness_catalog.seed_builtin_exercises()
    bench = _exercise("卧推")
    invalid = _plan(bench["id"])
    invalid["days"][0]["exercises"].append({
        "exercise_id": bench["id"],
        "sort_order": 1,
        "sets": 2,
        "rep_min": 10,
        "rep_max": 12,
    })
    with pytest.raises(ValueError, match="动作顺序不能重复"):
        fitness_training.create_plan("owner", invalid)


def test_chat_set_parser_is_conservative():
    assert fitness_training.parse_chat_set("记录组：卧推 60kg x 8 RIR2") == {
        "exercise_name": "卧推",
        "weight_kg": 60.0,
        "reps": 8,
        "rir": 2.0,
    }
    assert fitness_training.parse_chat_set("今天练得不错") is None
