"""从本地 JSON 导入食品营养目录，不联网、不导入个人饮食记录。

支持 USDA FoodData Central 常见的数组/foods 包装，以及 Open Food Facts
products 包装。许可证和署名必须由调用方显式提供，导入后会写入来源审计记录。

用法：
  python server/scripts/import_fitness_foods.py foods.json \
      --source usda-fdc --license CC0 --attribution "USDA FoodData Central"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许从仓库根目录直接运行脚本。
ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from app.core.memory import owner_user_id  # noqa: E402
from app.models.database import init_db  # noqa: E402
from app.services import fitness_catalog, fitness_nutrition  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="导入本地食品营养 JSON")
    parser.add_argument("path", type=Path, help="食品 JSON 文件路径")
    parser.add_argument("--source", default="external", help="数据源标识，如 usda-fdc/open-food-facts")
    parser.add_argument("--license", dest="license_name", default="", help="数据许可证")
    parser.add_argument("--attribution", default="", help="数据署名")
    parser.add_argument("--external-id", default=None, help="本次导入的外部批次 ID")
    parser.add_argument("--skip-invalid", action="store_true", help="跳过格式错误的记录并在输出中报告")
    args = parser.parse_args()
    if not args.path.is_file():
        parser.error(f"文件不存在：{args.path}")
    try:
        payload = json.loads(args.path.read_text(encoding="utf-8"))
        records = fitness_nutrition.load_food_records(payload)
        init_db()
        result = fitness_nutrition.import_foods(
            records,
            source=args.source,
            license_name=args.license_name,
            attribution=args.attribution,
            skip_invalid=args.skip_invalid,
        )
        audit = fitness_catalog.record_import(
            owner_user_id(),
            source=args.source,
            external_id=args.external_id,
            content_hash=fitness_catalog.content_hash(records),
            status="imported",
            imported_count=result["imported"],
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, KeyError) as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps({**result, "import_id": audit["id"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
