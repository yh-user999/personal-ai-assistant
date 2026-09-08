"""从本地 JSON 导入食品营养目录，不联网、不导入个人饮食记录。

支持 USDA FoodData Central 常见的数组/foods 包装，以及 Open Food Facts
products 包装。许可证和署名必须由调用方显式提供，导入后会写入来源审计记录。

用法：
  python server/scripts/import_fitness_foods.py foods.json \
      --source usda-fdc --license CC0 --attribution "USDA FoodData Central"
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

# 允许从仓库根目录直接运行脚本。
ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from app.core.memory import owner_user_id  # noqa: E402
from app.models.database import init_db  # noqa: E402
from app.services import fitness_catalog, fitness_nutrition  # noqa: E402


def _read_payload(path: Path, member: str | None = None) -> Any:
    """读取 JSON 或包含 JSON 的 USDA ZIP；不解压到磁盘。"""
    if path.suffix.casefold() != ".zip":
        return json.loads(path.read_text(encoding="utf-8"))
    with zipfile.ZipFile(path) as archive:
        names = sorted(
            name for name in archive.namelist()
            if name.casefold().endswith(".json") and not name.endswith("/")
        )
        if member:
            if member not in names:
                raise ValueError(f"ZIP 中不存在 JSON 成员：{member}")
            selected = member
        else:
            preferred = [name for name in names if "foundation_food" in name.casefold()]
            selected = (preferred or names)[0] if names else ""
        if not selected:
            raise ValueError("ZIP 中没有 JSON 数据文件")
        with archive.open(selected) as raw:
            return json.load(io.TextIOWrapper(raw, encoding="utf-8"))


def _empty_result() -> dict[str, Any]:
    return {"created": 0, "updated": 0, "imported": 0, "skipped": 0, "total": 0}


def _import_batches(records, *, args) -> dict[str, Any]:
    result = _empty_result()
    errors: list[str] = []
    for offset in range(0, len(records), args.batch_size):
        batch = records[offset:offset + args.batch_size]
        current = fitness_nutrition.import_foods(
            batch,
            source=args.source,
            license_name=args.license_name,
            attribution=args.attribution,
            skip_invalid=args.skip_invalid,
        )
        for key in ("created", "updated", "imported", "skipped"):
            result[key] += int(current.get(key, 0))
        result["total"] += int(current.get("total", len(batch)))
        errors.extend(f"批次起始第 {offset + 1} 条：{item}" for item in current.get("errors", []))
    if errors:
        result["errors"] = errors[:20]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="导入本地食品营养 JSON 或 USDA ZIP")
    parser.add_argument("path", type=Path, help="食品 JSON 或包含 JSON 的 ZIP 文件路径")
    parser.add_argument("--source", default="external", help="数据源标识，如 usda-fdc/open-food-facts")
    parser.add_argument("--license", dest="license_name", default="", help="数据许可证")
    parser.add_argument("--attribution", default="", help="数据署名")
    parser.add_argument("--external-id", default=None, help="本次导入的外部批次 ID")
    parser.add_argument("--member", default=None, help="ZIP 内要读取的 JSON 成员名")
    parser.add_argument(
        "--max-records",
        type=int,
        default=fitness_nutrition.MAX_IMPORT_RECORDS,
        help=f"单次最多读取记录数（默认 {fitness_nutrition.MAX_IMPORT_RECORDS}）",
    )
    parser.add_argument("--batch-size", type=int, default=1000, help="分批写入大小（默认 1000）")
    parser.add_argument("--skip-invalid", action="store_true", help="跳过格式错误的记录并在输出中报告")
    args = parser.parse_args()
    if not args.path.is_file():
        parser.error(f"文件不存在：{args.path}")
    if args.max_records < 1:
        parser.error("--max-records 必须大于 0")
    if args.batch_size < 1 or args.batch_size > fitness_nutrition.MAX_IMPORT_RECORDS:
        parser.error(f"--batch-size 必须在 1 到 {fitness_nutrition.MAX_IMPORT_RECORDS} 之间")
    try:
        payload = _read_payload(args.path, args.member)
        records = fitness_nutrition.load_food_records(payload, max_records=args.max_records)
        init_db()
        if not args.skip_invalid:
            for index, record in enumerate(records, 1):
                try:
                    fitness_nutrition.normalize_food_record(
                        record,
                        source=args.source,
                        license_name=args.license_name,
                        attribution=args.attribution,
                    )
                except ValueError as exc:
                    raise ValueError(f"第 {index} 条：{exc}") from exc
        result = _import_batches(records, args=args)
        audit = fitness_catalog.record_import(
            owner_user_id(),
            source=args.source,
            external_id=args.external_id,
            content_hash=fitness_catalog.content_hash(records),
            status="imported",
            imported_count=result["imported"],
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps({**result, "import_id": audit["id"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
