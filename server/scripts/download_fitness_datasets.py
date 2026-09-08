"""下载并校验公开健身/营养数据集，不触碰个人数据库。

默认下载：
- yuhonas/free-exercise-db 的合并动作 JSON；
- USDA FoodData Central 当前 Foundation Foods JSON 压缩包（2025-04-24）。

文件写入 ``server/data/fitness-datasets``，该目录已被 gitignore 排除。下载使用临时文件
和 SHA-256 校验，失败时不会覆盖已有完整文件；服务运行期间不会调用本脚本或访问外部数据源。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = ROOT / "server" / "data" / "fitness-datasets"
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
USER_AGENT = "personal-ai-assistant-fitness-datasets/1.0"


@dataclass(frozen=True)
class DatasetDefinition:
    key: str
    url: str
    filename: str
    kind: str
    license_name: str
    attribution: str


DATASETS: dict[str, DatasetDefinition] = {
    "free-exercise-db": DatasetDefinition(
        key="free-exercise-db",
        url="https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/dist/exercises.json",
        filename="free-exercise-db-exercises.json",
        kind="json",
        license_name="Unlicense",
        attribution="yuhonas/free-exercise-db",
    ),
    "usda-foundation-foods": DatasetDefinition(
        key="usda-foundation-foods",
        url="https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_foundation_food_json_2025-04-24.zip",
        filename="FoodData_Central_foundation_food_json_2025-04-24.zip",
        kind="zip",
        license_name="CC0",
        attribution="USDA FoodData Central",
    ),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_metadata(path: Path) -> dict[str, Any]:
    return {
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _download(url: str, destination: Path, *, max_bytes: int, force: bool) -> dict[str, Any]:
    """下载单个文件，返回脱敏文件元数据。"""
    if destination.exists() and not force:
        metadata = _file_metadata(destination)
        metadata["downloaded"] = False
        return metadata

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.part")
    temporary.unlink(missing_ok=True)
    request = Request(url, headers={"User-Agent": USER_AGENT})
    total = 0
    digest = hashlib.sha256()
    try:
        with urlopen(request, timeout=120) as response, temporary.open("wb") as handle:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise ValueError(f"下载文件超过上限 {max_bytes} 字节")
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"下载文件超过上限 {max_bytes} 字节")
                handle.write(chunk)
                digest.update(chunk)
        if total == 0:
            raise ValueError("下载文件为空")
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "filename": destination.name,
        "size_bytes": total,
        "sha256": digest.hexdigest(),
        "downloaded": True,
    }


def _json_record_count(payload: Any) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("exercises", "foods", "FoundationFoods", "products", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
    raise ValueError("数据集 JSON 顶层结构无法识别")


def _validate(path: Path, kind: str) -> dict[str, Any]:
    if kind == "json":
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return {"format": "json", "record_count": _json_record_count(payload)}
    if kind == "zip":
        with zipfile.ZipFile(path) as archive:
            bad_member = archive.testzip()
            if bad_member:
                raise ValueError(f"ZIP 校验失败：{bad_member}")
            members = sorted(
                name for name in archive.namelist()
                if name.lower().endswith(".json") and not name.endswith("/")
            )
            if not members:
                raise ValueError("ZIP 中没有 JSON 数据文件")
            return {"format": "zip", "json_members": members[:20], "json_member_count": len(members)}
    raise ValueError(f"不支持的数据集格式：{kind}")


def _write_json(path: Path, payload: Any) -> None:
    temporary = path.with_name(f".{path.name}.part")
    temporary.unlink(missing_ok=True)
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def download_datasets(
    dataset_keys: list[str],
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    force: bool = False,
    max_bytes: int = DEFAULT_MAX_BYTES,
    url_override: str | None = None,
) -> dict[str, Any]:
    if max_bytes <= 0:
        raise ValueError("max_bytes 必须大于 0")
    if url_override and len(dataset_keys) != 1:
        raise ValueError("--url 只能与一个 --dataset 一起使用")
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for key in dataset_keys:
        definition = DATASETS[key]
        url = url_override or definition.url
        destination = output_dir / definition.filename
        metadata = _download(url, destination, max_bytes=max_bytes, force=force)
        validation = _validate(destination, definition.kind)
        results.append(
            {
                **asdict(definition),
                "url": url,
                "path": destination.name,
                **metadata,
                "validation": validation,
            }
        )
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "datasets": results,
    }
    _write_json(output_dir / "manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="下载并校验公开健身/营养数据集")
    parser.add_argument(
        "--dataset",
        action="append",
        choices=tuple(DATASETS),
        dest="dataset_keys",
        help="要下载的数据集；可重复传入，默认下载全部",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="下载目录（默认 server/data/fitness-datasets）",
    )
    parser.add_argument("--url", default=None, help="单数据集临时覆盖 URL（不写入 manifest 以外的配置）")
    parser.add_argument("--force", action="store_true", help="重新下载并原子替换已有文件")
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help=f"单文件最大字节数（默认 {DEFAULT_MAX_BYTES}）",
    )
    args = parser.parse_args()
    keys = args.dataset_keys or list(DATASETS)
    try:
        result = download_datasets(
            keys,
            output_dir=args.output_dir,
            force=args.force,
            max_bytes=args.max_bytes,
            url_override=args.url,
        )
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        print(f"下载失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
