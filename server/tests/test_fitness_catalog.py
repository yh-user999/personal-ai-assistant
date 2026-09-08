"""结构化动作目录测试。"""
from app.services import fitness_catalog


def test_normalize_free_exercise_db_shape():
    item = fitness_catalog.normalize_exercise_record(
        {
            "id": "bench-1",
            "name": "卧推",
            "primaryMuscles": ["chest"],
            "secondaryMuscles": ["triceps"],
            "equipment": "barbell",
            "instructions": ["保持肩胛稳定"],
            "images": ["https://example.invalid/bench.png"],
        },
        source="free-exercise-db",
        license_name="Unlicense",
        attribution="公开动作数据",
    )
    assert item["source"] == "free-exercise-db"
    assert item["source_id"] == "bench-1"
    assert item["primary_muscles"] == ["chest"]
    assert item["license"] == "Unlicense"


def test_import_is_idempotent_and_searchable(db):
    records = [
        {"id": "custom-bench", "name": "测试卧推", "primaryMuscles": ["胸"]},
    ]
    first = fitness_catalog.import_exercises(
        records,
        source="test",
        license_name="MIT",
        attribution="test",
    )
    second = fitness_catalog.import_exercises(
        [{**records[0], "aliases": ["卧推"]}],
        source="test",
        license_name="MIT",
        attribution="test",
    )
    assert first == {"created": 1, "updated": 0, "total": 1}
    assert second == {"created": 0, "updated": 1, "total": 1}
    matches = fitness_catalog.list_exercises(query="测试卧推", limit=10)
    assert len(matches) == 1
    assert matches[0]["aliases"] == ["卧推"]
    assert matches[0]["license"] == "MIT"


def test_builtin_seed_and_import_record(db):
    result = fitness_catalog.seed_builtin_exercises()
    assert result["created"] >= 8
    assert fitness_catalog.ensure_builtin_exercises() is None
    record = fitness_catalog.record_import(
        "owner",
        source="test",
        external_id=None,
        content_hash="abc123",
        status="preview",
        imported_count=2,
    )
    assert record["source"] == "test"
    assert record["status"] == "preview"
    assert record["imported_count"] == 2
