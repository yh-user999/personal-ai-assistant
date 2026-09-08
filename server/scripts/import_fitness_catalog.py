"""从本地 JSON 导入健身动作目录，不联网、不导入个人训练记录。

支持 Free Exercise DB 风格的顶层数组或 {"exercises": [...]} 包装；
wrkout/exercises.json 等数据集导入前请先核对仓库 LICENSE 和媒体授权。

用法：
  python server/scripts/import_fitness_catalog.py path/to/exercises.json \
      --source free-exercise-db --license Unlicense --attribution "..."
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
from app.services import fitness_catalog  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="导入本地健身动作 JSON，并记录来源审计")
    parser.add_argument("path", type=Path, help="动作 JSON 文件路径")
    parser.add_argument("--source", default="external", help="数据源标识")
    parser.add_argument("--license", dest="license_name", default="", help="数据许可证")
    parser.add_argument("--attribution", default="", help="数据署名")
    parser.add_argument("--external-id", default=None, help="本次导入的外部批次 ID")
    parser.add_argument("--max-records", type=int, default=5000, help="单次最多导入动作数（默认 5000）")
    args = parser.parse_args()
    if not args.path.is_file():
        parser.error(f"文件不存在：{args.path}")
    if args.max_records < 1:
        parser.error("--max-records 必须大于 0")
    try:
        payload = json.loads(args.path.read_text(encoding="utf-8"))
        records = fitness_catalog.load_json_records(payload)
        if len(records) > args.max_records:
            raise ValueError(f"单次最多导入 {args.max_records} 个动作")
        init_db()
        result = fitness_catalog.import_exercises(
            records,
            source=args.source,
            license_name=args.license_name,
            attribution=args.attribution,
        )
        audit = fitness_catalog.record_import(
            owner_user_id(),
            source=args.source,
            external_id=args.external_id,
            content_hash=fitness_catalog.content_hash(records),
            status="imported",
            imported_count=result["total"],
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, KeyError) as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps({**result, "import_id": audit["id"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
