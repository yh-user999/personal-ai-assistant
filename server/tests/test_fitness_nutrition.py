"""食品营养目录、营养计算和用户饮食记录测试。"""
import pytest

from app.services import fitness_nutrition


def usda_oats():
    return {
        "fdcId": "fdc-oats-1",
        "description": "燕麦片",
        "foodNutrients": [
            {"nutrientId": 1008, "nutrientName": "Energy", "unitName": "KCAL", "value": 389},
            {"nutrientId": 1003, "nutrientName": "Protein", "unitName": "G", "value": 16.9},
            {"nutrientId": 1004, "nutrientName": "Total lipid (fat)", "unitName": "G", "value": 6.9},
            {"nutrientId": 1005, "nutrientName": "Carbohydrate", "unitName": "G", "value": 66.3},
            {"nutrientId": 1079, "nutrientName": "Fiber", "unitName": "G", "value": 10.6},
            {"nutrientId": 1093, "nutrientName": "Sodium", "unitName": "MG", "value": 2},
        ],
    }


def test_load_food_records_accepts_common_wrappers():
    assert len(fitness_nutrition.load_food_records({"foods": [usda_oats()]})) == 1
    assert len(fitness_nutrition.load_food_records({"products": [{"id": "p", "name": "食品"}]})) == 1
    with pytest.raises(ValueError, match="食品 JSON"):
        fitness_nutrition.load_food_records({"unexpected": []})


def test_normalize_usda_fooddata_shape():
    item = fitness_nutrition.normalize_food_record(
        usda_oats(),
        source="usda-fdc",
        license_name="CC0",
        attribution="USDA FoodData Central",
    )
    assert item["source_id"] == "fdc-oats-1"
    assert item["name"] == "燕麦片"
    assert item["calories_kcal"] == 389
    assert item["protein_g"] == 16.9
    assert item["license"] == "CC0"


def test_normalize_open_food_facts_shape():
    item = fitness_nutrition.normalize_food_record(
        {
            "code": "690000000001",
            "product_name": "无糖酸奶",
            "brands": "测试品牌",
            "nutriments": {
                "energy-kcal_100g": 62,
                "proteins_100g": 3.8,
                "fat_100g": 3.2,
                "carbohydrates_100g": 4.5,
                "fiber_100g": 0,
                "sodium_100g": 0.05,
            },
        },
        source="open-food-facts",
        license_name="ODbL-1.0",
        attribution="Open Food Facts",
    )
    assert item["source_id"] == "690000000001"
    assert item["brand"] == "测试品牌"
    assert item["calories_kcal"] == 62
    assert item["protein_g"] == 3.8
    assert item["sodium_mg"] == 50


def test_import_is_idempotent_searchable_and_calculates(db):
    first = fitness_nutrition.import_foods(
        [usda_oats()],
        source="usda-fdc",
        license_name="CC0",
        attribution="USDA",
    )
    second = fitness_nutrition.import_foods(
        [{**usda_oats(), "description": "燕麦片（更新）"}],
        source="usda-fdc",
        license_name="CC0",
        attribution="USDA",
    )
    assert first["created"] == 1
    assert second["updated"] == 1
    food = fitness_nutrition.list_foods(query="更新", source="usda-fdc")
    assert len(food) == 1
    nutrition = fitness_nutrition.calculate_nutrition(food[0], 50)
    assert nutrition["grams"] == 50
    assert nutrition["calories_kcal"] == 194.5
    assert nutrition["protein_g"] == 8.45


def test_invalid_food_can_be_skipped(db):
    result = fitness_nutrition.import_foods(
        [{"id": "ok", "name": "鸡蛋", "calories": 143}, {"calories": 143}],
        source="test",
        skip_invalid=True,
    )
    assert result["imported"] == 1
    assert result["skipped"] == 1
    assert result["errors"]


def test_food_logs_are_user_scoped_idempotent_and_summarized(db):
    fitness_nutrition.import_foods([usda_oats()], source="usda-fdc")
    food = fitness_nutrition.list_foods(query="燕麦")[0]
    first = fitness_nutrition.log_food(
        "owner",
        food["id"],
        grams=50,
        meal="早餐",
        source="test",
        external_id="meal-1",
        eaten_at="2026-09-08T08:00:00+00:00",
    )
    second = fitness_nutrition.log_food(
        "owner",
        food["id"],
        grams=100,
        meal="早餐",
        source="test",
        external_id="meal-1",
        eaten_at="2026-09-08T08:00:00+00:00",
    )
    other = fitness_nutrition.log_food(
        "20002",
        food["id"],
        grams=25,
        meal="早餐",
        source="test",
        eaten_at="2026-09-08T08:00:00+00:00",
    )
    assert first["id"] == second["id"]
    assert second["grams"] == 100
    assert other["user_id"] == "20002"
    assert len(fitness_nutrition.list_food_logs("owner", date="2026-09-08")) == 1
    assert len(fitness_nutrition.list_food_logs("20002", date="2026-09-08")) == 1
    summary = fitness_nutrition.nutrition_summary("owner", date="2026-09-08")
    assert summary["log_count"] == 1
    assert summary["calories_kcal"] == 389
    assert summary["protein_g"] == 16.9


def test_food_log_validation_and_missing_food(db):
    with pytest.raises(KeyError, match="食品不存在"):
        fitness_nutrition.log_food("owner", 999, grams=50)
    with pytest.raises(ValueError, match="日期"):
        fitness_nutrition.list_food_logs("owner", date="2026/09/08")
    with pytest.raises(ValueError, match="克数"):
        fitness_nutrition.calculate_nutrition({"calories_kcal": 100}, 0)
