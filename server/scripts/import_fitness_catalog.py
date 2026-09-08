"""从本地 JSON 导入健身动作目录，不联网、不导入个人训练记录。

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

from app.models.database import init_db  # noqa: E402
from app.services.fitness_catalog import import_exercises, load_json_records  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="导入本地健身动作 JSON")
    parser.add_argument("path", type=Path, help="动作 JSON 文件路径")
    parser.add_argument("--source", default="external", help="数据源标识")
    parser.add_argument("--license", dest="license_name", default="", help="数据许可证")
    parser.add_argument("--attribution", default="", help="数据署名")
    args = parser.parse_args()
    if not args.path.is_file():
        parser.error(f"文件不存在：{args.path}")
    try:
        payload = json.loads(args.path.read_text(encoding="utf-8"))
        records = load_json_records(payload)
        init_db()
        result = import_exercises(
            records,
            source=args.source,
            license_name=args.license_name,
            attribution=args.attribution,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
