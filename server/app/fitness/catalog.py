"""结构化健身动作目录。

动作目录只保存公开动作元数据；个人训练记录不属于目录，也不会被导入脚本写入。
外部数据源通过本地 JSON 导入，服务本身不联网下载第三方数据。
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from app.common.timeutil import utc_iso
from app.models.database import db_connection
from app.services.sanitize import sanitize

MAX_NAME_CHARS = 160
MAX_ALIAS_CHARS = 80
MAX_LIST_ITEMS = 32
MAX_ITEM_CHARS = 160
MAX_INSTRUCTION_CHARS = 600
MAX_IMAGE_CHARS = 500
MAX_SOURCE_CHARS = 80
MAX_SOURCE_ID_CHARS = 160
_BUILTIN_LOCK = threading.Lock()


BUILTIN_EXERCISES: tuple[dict[str, Any], ...] = (
    {
        "id": "builtin-squat",
        "name": "深蹲",
        "aliases": ["杠铃深蹲", "Back Squat", "Squat"],
        "primaryMuscles": ["quadriceps", "glutes"],
        "secondaryMuscles": ["hamstrings", "core"],
        "equipment": "barbell",
        "instructions": ["保持躯干稳定，膝盖与脚尖方向一致。", "下蹲到舒适深度后稳定起身。"],
    },
    {
        "id": "builtin-bench-press",
        "name": "卧推",
        "aliases": ["平板卧推", "Bench Press"],
        "primaryMuscles": ["chest"],
        "secondaryMuscles": ["triceps", "shoulders"],
        "equipment": "barbell",
        "instructions": ["肩胛骨收紧，双脚稳定支撑。", "杠铃下放到胸部舒适位置后推起。"],
    },
    {
        "id": "builtin-row",
        "name": "俯身杠铃划船",
        "aliases": ["杠铃划船", "Barbell Row", "Bent Over Row"],
        "primaryMuscles": ["back"],
        "secondaryMuscles": ["biceps", "rear deltoids"],
        "equipment": "barbell",
        "instructions": ["保持脊柱中立，肩胛骨控制回收。", "将杠铃拉向上腹部，避免借力甩动。"],
    },
    {
        "id": "builtin-lat-pulldown",
        "name": "高位下拉",
        "aliases": ["下拉", "Lat Pulldown"],
        "primaryMuscles": ["lats"],
        "secondaryMuscles": ["biceps", "back"],
        "equipment": "cable",
        "instructions": ["坐稳并保持胸口打开。", "把手拉向上胸，控制回放。"],
    },
    {
        "id": "builtin-overhead-press",
        "name": "站姿肩推",
        "aliases": ["杠铃肩推", "Overhead Press"],
        "primaryMuscles": ["shoulders"],
        "secondaryMuscles": ["triceps", "core"],
        "equipment": "barbell",
        "instructions": ["收紧核心，避免腰部过度后仰。", "沿稳定路径将重量推过头顶。"],
    },
    {
        "id": "builtin-dumbbell-curl",
        "name": "哑铃弯举",
        "aliases": ["二头弯举", "Dumbbell Curl"],
        "primaryMuscles": ["biceps"],
        "secondaryMuscles": [],
        "equipment": "dumbbell",
        "instructions": ["上臂保持稳定，只让前臂完成弯举。", "下放时控制速度，不要甩动。"],
    },
    {
        "id": "builtin-triceps-pushdown",
        "name": "绳索下压",
        "aliases": ["三头下压", "Triceps Pushdown"],
        "primaryMuscles": ["triceps"],
        "secondaryMuscles": [],
        "equipment": "cable",
        "instructions": ["肘部贴近身体，肩膀保持放松。", "下压到手臂接近伸直后控制回放。"],
    },
    {
        "id": "builtin-plank",
        "name": "平板支撑",
        "aliases": ["Plank"],
        "primaryMuscles": ["core"],
        "secondaryMuscles": ["shoulders"],
        "equipment": "bodyweight",
        "instructions": ["保持头、躯干和髋部在一条线上。", "正常呼吸，不要塌腰或抬臀。"],
    },
)


def _text(value: Any, *, limit: int, required: bool = False) -> str:
    value = sanitize(str(value or "")).strip()
    if required and not value:
        raise ValueError("动作名称不能为空")
    return value[:limit]


def _items(value: Any, *, limit: int, item_limit: int) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw: Sequence[Any] = value.replace("，", ",").split(",")
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        raw = value
    else:
        raw = [value]
    result: list[str] = []
    for item in raw[:limit]:
        text = _text(item, limit=item_limit)
        if text and text not in result:
            result.append(text)
    return result


def _json(value: Any, default: list[str] | None = None) -> str:
    return json.dumps(value if value is not None else (default or []), ensure_ascii=False, separators=(",", ":"))


def normalize_exercise_record(
    record: Mapping[str, Any],
    *,
    source: str = "external",
    license_name: str = "",
    attribution: str = "",
) -> dict[str, Any]:
    """把 Free Exercise DB 风格或内部 snake_case 记录规范化。"""
    if not isinstance(record, Mapping):
        raise ValueError("动作记录必须是对象")
    source_value = _text(record.get("source") or source, limit=MAX_SOURCE_CHARS, required=True)
    source_id = _text(
        record.get("source_id") or record.get("id") or record.get("slug") or record.get("name"),
        limit=MAX_SOURCE_ID_CHARS,
        required=True,
    )
    name = _text(record.get("name") or record.get("title"), limit=MAX_NAME_CHARS, required=True)
    aliases = _items(record.get("aliases") or record.get("alias"), limit=MAX_LIST_ITEMS, item_limit=MAX_ALIAS_CHARS)
    primary = _items(
        record.get("primary_muscles") or record.get("primaryMuscles"),
        limit=MAX_LIST_ITEMS,
        item_limit=MAX_ITEM_CHARS,
    )
    secondary = _items(
        record.get("secondary_muscles") or record.get("secondaryMuscles"),
        limit=MAX_LIST_ITEMS,
        item_limit=MAX_ITEM_CHARS,
    )
    instructions = _items(
        record.get("instructions") or record.get("steps"),
        limit=MAX_LIST_ITEMS,
        item_limit=MAX_INSTRUCTION_CHARS,
    )
    images = _items(
        record.get("images") or record.get("image"),
        limit=MAX_LIST_ITEMS,
        item_limit=MAX_IMAGE_CHARS,
    )
    return {
        "source": source_value,
        "source_id": source_id,
        "name": name,
        "aliases": aliases,
        "primary_muscles": primary,
        "secondary_muscles": secondary,
        "equipment": _text(record.get("equipment"), limit=MAX_ITEM_CHARS),
        "instructions": instructions,
        "images": images,
        "license": _text(record.get("license") or record.get("license_name") or license_name, limit=MAX_ITEM_CHARS),
        "attribution": _text(record.get("attribution") or attribution, limit=MAX_ITEM_CHARS),
    }


def _row_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key in ("aliases", "primary_muscles", "secondary_muscles", "instructions", "images"):
        try:
            result[key] = json.loads(result.get(key) or "[]")
        except (TypeError, json.JSONDecodeError):
            result[key] = []
    return result


def import_exercises(
    records: Iterable[Mapping[str, Any]],
    *,
    source: str = "external",
    license_name: str = "",
    attribution: str = "",
) -> dict[str, int]:
    """幂等导入动作；同一 source/source_id 已存在时更新。"""
    normalized = [
        normalize_exercise_record(
            record,
            source=source,
            license_name=license_name,
            attribution=attribution,
        )
        for record in records
    ]
    now = utc_iso()
    created = updated = 0
    with db_connection() as conn:
        for item in normalized:
            old = conn.execute(
                "SELECT id FROM fitness_exercises WHERE source=? AND source_id=?",
                (item["source"], item["source_id"]),
            ).fetchone()
            values = (
                item["source"],
                item["source_id"],
                item["name"],
                _json(item["aliases"]),
                _json(item["primary_muscles"]),
                _json(item["secondary_muscles"]),
                item["equipment"],
                _json(item["instructions"]),
                _json(item["images"]),
                item["license"],
                item["attribution"],
                now,
            )
            if old:
                conn.execute(
                    "UPDATE fitness_exercises SET name=?, aliases=?, primary_muscles=?, secondary_muscles=?, "
                    "equipment=?, instructions=?, images=?, license=?, attribution=?, updated_at=? WHERE id=?",
                    values[2:] + (old["id"],),
                )
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO fitness_exercises "
                    "(source, source_id, name, aliases, primary_muscles, secondary_muscles, equipment, "
                    "instructions, images, license, attribution, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        values[:11] + (now, now),
                )
                created += 1
    return {"created": created, "updated": updated, "total": len(normalized)}


def seed_builtin_exercises() -> dict[str, int]:
    return import_exercises(
        BUILTIN_EXERCISES,
        source="builtin",
        license_name="应用内种子数据",
        attribution="Personal AI Assistant",
    )


def ensure_builtin_exercises() -> None:
    with _BUILTIN_LOCK:
        with db_connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM fitness_exercises WHERE source='builtin' LIMIT 1"
            ).fetchone()
        if not exists:
            seed_builtin_exercises()


def list_exercises(
    *,
    query: str = "",
    muscle: str = "",
    equipment: str = "",
    limit: int = 20,
) -> list[dict[str, Any]]:
    ensure_builtin_exercises()
    limit = max(1, min(int(limit), 100))
    terms = [value.strip() for value in (query, muscle, equipment)]
    clauses: list[str] = []
    args: list[Any] = []
    if terms[0]:
        like = f"%{terms[0][:MAX_NAME_CHARS]}%"
        clauses.append("(name LIKE ? OR aliases LIKE ? OR primary_muscles LIKE ? OR secondary_muscles LIKE ?)")
        args.extend([like, like, like, like])
    if terms[1]:
        clauses.append("(primary_muscles LIKE ? OR secondary_muscles LIKE ?)")
        like = f"%{terms[1][:MAX_ITEM_CHARS]}%"
        args.extend([like, like])
    if terms[2]:
        clauses.append("equipment LIKE ?")
        args.append(f"%{terms[2][:MAX_ITEM_CHARS]}%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT id, source, source_id, name, aliases, primary_muscles, secondary_muscles, equipment, "
            "instructions, images, license, attribution, created_at, updated_at "
            f"FROM fitness_exercises {where} ORDER BY name COLLATE NOCASE, id LIMIT ?",
            args + [limit],
        ).fetchall()
    return [_row_payload(row) for row in rows]


def get_exercise(exercise_id: int) -> dict[str, Any] | None:
    ensure_builtin_exercises()
    with db_connection() as conn:
        row = conn.execute(
            "SELECT id, source, source_id, name, aliases, primary_muscles, secondary_muscles, equipment, "
            "instructions, images, license, attribution, created_at, updated_at "
            "FROM fitness_exercises WHERE id=?",
            (int(exercise_id),),
        ).fetchone()
    return _row_payload(row) if row else None


def load_json_records(payload: Any) -> list[Mapping[str, Any]]:
    """读取动作导入 JSON；允许顶层数组或 {exercises: [...]}。"""
    if isinstance(payload, Mapping):
        payload = payload.get("exercises")
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes, bytearray)):
        raise ValueError("动作 JSON 必须是数组或包含 exercises 数组的对象")
    records = list(payload)
    if len(records) > 5000:
        raise ValueError("单次最多导入 5000 个动作")
    if not all(isinstance(item, Mapping) for item in records):
        raise ValueError("动作数组中的每一项必须是对象")
    return records


def record_import(
    user_id: str | None,
    *,
    source: str,
    external_id: str | None,
    content_hash: str,
    status: str,
    imported_count: int,
) -> dict[str, Any]:
    from app.core.memory import normalize_user_id

    uid = normalize_user_id(user_id)
    source = _text(source, limit=MAX_SOURCE_CHARS, required=True)
    external_id = _text(external_id, limit=200) or None
    content_hash = _text(content_hash, limit=128)
    status = _text(status, limit=40, required=True)
    imported_count = max(0, min(int(imported_count), 5000))
    now = utc_iso()
    with db_connection() as conn:
        conn.execute(
            "INSERT INTO fitness_imports(user_id, source, external_id, content_hash, status, imported_count, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, source, external_id, content_hash) DO UPDATE SET status=excluded.status, "
            "imported_count=excluded.imported_count, updated_at=excluded.updated_at",
            (uid, source, external_id, content_hash, status, imported_count, now, now),
        )
        row = conn.execute(
            "SELECT id, user_id, source, external_id, content_hash, status, imported_count, created_at, updated_at "
            "FROM fitness_imports WHERE user_id=? AND source=? AND external_id IS ? AND content_hash=?",
            (uid, source, external_id, content_hash),
        ).fetchone()
    return dict(row)


def content_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
