"""公开健身/营养数据集下载与 ZIP 导入脚本测试。"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

from scripts import download_fitness_datasets as downloader
from scripts import import_fitness_foods
from app.services import fitness_nutrition


class _Response(io.BytesIO):
    def __init__(self, payload: bytes):
        super().__init__(payload)
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def test_downloader_uses_atomic_file_and_manifest(tmp_path, monkeypatch):
    payload = b'[{"id":"squat","name":"Deep Squat"}]'
    monkeypatch.setattr(downloader, "urlopen", lambda request, timeout: _Response(payload))

    manifest = downloader.download_datasets(
        ["free-exercise-db"],
        output_dir=tmp_path,
        force=True,
        url_override="https://example.invalid/exercises.json",
    )
    output = tmp_path / "free-exercise-db-exercises.json"
    assert output.read_bytes() == payload
    assert manifest["datasets"][0]["validation"]["record_count"] == 1
    assert len(manifest["datasets"][0]["sha256"]) == 64
    assert json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))["datasets"]
    assert not list(tmp_path.glob("*.part"))


def test_downloader_rejects_oversized_payload_without_replacing_file(tmp_path, monkeypatch):
    destination = tmp_path / "file.json"
    destination.write_bytes(b"old")
    monkeypatch.setattr(downloader, "urlopen", lambda request, timeout: _Response(b"too large"))

    try:
        downloader._download("https://example.invalid/file", destination, max_bytes=3, force=True)
    except ValueError as exc:
        assert "超过上限" in str(exc)
    else:
        raise AssertionError("应拒绝超过上限的下载")
    assert destination.read_bytes() == b"old"
    assert not (tmp_path / ".file.json.part").exists()


def _write_food_zip(path: Path, payload: object, member: str = "FoodData_Central_foundation_food_json.json"):
    content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, content)


def test_food_import_script_reads_usda_zip_and_batches(db, tmp_path, monkeypatch, capsys):
    path = tmp_path / "foods.zip"
    _write_food_zip(
        path,
        {
            "FoundationFoods": "ignored",
            "foods": [
                {"fdcId": "1", "description": "米饭", "calories": 116},
                {"fdcId": "2", "description": "鸡蛋", "calories": 143},
                {"fdcId": "3", "description": "燕麦", "calories": 389},
            ],
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "import_fitness_foods.py",
            str(path),
            "--source",
            "usda-fdc",
            "--license",
            "CC0",
            "--batch-size",
            "2",
        ],
    )
    assert import_fitness_foods.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["created"] == 3
    assert result["imported"] == 3
    assert len(fitness_nutrition.list_foods(source="usda-fdc", limit=10)) == 3


def test_food_import_prevalidates_before_writing(db, tmp_path, monkeypatch, capsys):
    path = tmp_path / "foods.json"
    path.write_text(
        json.dumps({"foods": [{"id": "ok", "name": "米饭"}, {"id": "bad"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["import_fitness_foods.py", str(path)])
    assert import_fitness_foods.main() == 1
    assert "第 2 条" in capsys.readouterr().err
    assert fitness_nutrition.list_foods(limit=10) == []
