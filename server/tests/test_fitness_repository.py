"""健身领域仓储的持久化契约测试。"""
from app.fitness.repository import SQLiteFitnessRepository


def test_repository_catalog_and_food_paths_are_idempotent(db):
    repo = SQLiteFitnessRepository()
    exercise = {
        "source": "test",
        "source_id": "bench-1",
        "name": "测试卧推",
        "aliases": ["卧推"],
        "primary_muscles": ["chest"],
        "secondary_muscles": ["triceps"],
        "equipment": "barbell",
        "instructions": ["稳定推起"],
        "images": [],
        "license": "test",
        "attribution": "test",
    }
    assert repo.upsert_exercises([exercise], now="2026-09-08T00:00:00+00:00") == {
        "created": 1,
        "updated": 0,
        "total": 1,
    }
    assert repo.upsert_exercises([exercise | {"aliases": ["平板卧推"]}], now="2026-09-08T00:01:00+00:00") == {
        "created": 0,
        "updated": 1,
        "total": 1,
    }
    item = repo.list_exercises(query="平板", limit=10)[0]
    assert item["aliases"] == ["平板卧推"]

    food = {
        "source": "test",
        "source_id": "food-1",
        "name": "测试燕麦",
        "aliases": [],
        "brand": "",
        "barcode": "",
        "serving_size_g": None,
        "calories_kcal": 389,
        "protein_g": 16.9,
        "fat_g": 6.9,
        "carbs_g": 66.3,
        "fiber_g": 10.6,
        "sodium_mg": 2,
        "nutrients": {},
        "license": "test",
        "attribution": "test",
    }
    assert repo.upsert_foods([food], now="2026-09-08T00:00:00+00:00")["created"] == 1
    food_id = repo.list_foods(query="测试燕麦")[0]["id"]
    logged = repo.upsert_food_log(
        "u1",
        food_id,
        source="test",
        external_id="meal-1",
        food=food,
        nutrition={
            "grams": 50,
            "calories_kcal": 194.5,
            "protein_g": 8.45,
            "fat_g": 3.45,
            "carbs_g": 33.15,
            "fiber_g": 5.3,
            "sodium_mg": 1,
        },
        meal="早餐",
        note="",
        eaten_at="2026-09-08T08:00:00+00:00",
        now="2026-09-08T08:00:00+00:00",
    )
    updated = repo.upsert_food_log(
        "u1",
        food_id,
        source="test",
        external_id="meal-1",
        food=food,
        nutrition={
            "grams": 100,
            "calories_kcal": 389,
            "protein_g": 16.9,
            "fat_g": 6.9,
            "carbs_g": 66.3,
            "fiber_g": 10.6,
            "sodium_mg": 2,
        },
        meal="早餐",
        note="更新",
        eaten_at="2026-09-08T08:00:00+00:00",
        now="2026-09-08T08:01:00+00:00",
    )
    assert updated["id"] == logged["id"]
    assert updated["grams"] == 100
    assert repo.nutrition_summary("u1", date="2026-09-08")["log_count"] == 1
    assert repo.list_food_logs("u2", date="2026-09-08") == []


def test_repository_training_paths_preserve_user_scope_and_legacy_mirror(db):
    repo = SQLiteFitnessRepository()
    exercise = {
        "source": "builtin",
        "source_id": "squat",
        "name": "深蹲",
        "aliases": [],
        "primary_muscles": ["legs"],
        "secondary_muscles": [],
        "equipment": "barbell",
        "instructions": [],
        "images": [],
        "license": "test",
        "attribution": "test",
    }
    repo.upsert_exercises([exercise], now="2026-09-08T00:00:00+00:00")
    exercise_id = repo.list_exercises(query="深蹲")[0]["id"]
    plan = repo.create_plan(
        "u1",
        name="测试计划",
        goal="训练",
        source="test",
        notes="",
        days=[{
            "day_index": 1,
            "name": "A",
            "notes": "",
            "exercises": [{
                "exercise_id": exercise_id,
                "sort_order": 1,
                "sets": 3,
                "rep_min": 8,
                "rep_max": 12,
                "rir_target": 2,
                "rest_seconds": None,
                "notes": "",
            }],
        }],
        now="2026-09-08T00:00:00+00:00",
    )
    assert plan and plan["user_id"] == "u1"
    assert repo.get_plan("u2", plan["id"]) is None
    session = repo.start_session(
        "u1",
        plan_id=plan["id"],
        plan_day_id=plan["days"][0]["id"],
        source="test",
        external_id="session-1",
        notes="",
        now="2026-09-08T08:00:00+00:00",
    )
    assert session
    logged = repo.log_set(
        "u1", session["id"], exercise_id,
        reps=8, weight_kg=60, set_order=None, set_type="working",
        rpe=None, rir=2, rest_seconds=None, is_warmup=False, note="",
        now="2026-09-08T08:05:00+00:00",
    )
    assert logged["exercise_name"] == "深蹲"
    completed = repo.complete_session("u1", session["id"], now="2026-09-08T08:30:00+00:00")
    assert completed and completed["status"] == "completed"
    measurement = repo.record_measurement(
        "u1", kind="weight", value=70.5, unit="kg", note="", measured_at="2026-09-08T08:00:00+00:00", now="2026-09-08T08:00:00+00:00"
    )
    assert measurement and measurement["value"] == 70.5
    assert repo.summary_rows("u1", since="2026-09-01T00:00:00+00:00")["legacy"][0]["kind"] == "weight"
