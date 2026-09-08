"""本地优先的食品营养目录与饮食记录服务。

服务只处理调用方提供的本地 JSON，不联网下载第三方数据。食品主表保存来源、许可证
和署名；饮食记录保存当时的营养快照，避免目录更新改变历史统计。
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from app.common.timeutil import utc_iso
from app.models.database import db_connection
from app.services.sanitize import sanitize

MAX_FOOD_NAME_CHARS = 200
MAX_ALIAS_CHARS = 100
MAX_LIST_ITEMS = 32
MAX_BRAND_CHARS = 160
MAX_SOURCE_CHARS = 80
MAX_SOURCE_ID_CHARS = 200
MAX_BARCODE_CHARS = 64
MAX_LICENSE_CHARS = 160
MAX_ATTRIBUTION_CHARS = 300
MAX_NUTRIENTS = 80
MAX_IMPORT_RECORDS = 50000
MAX_GRAMS = 5000.0
MAX_DATE_CHARS = 10
MAX_CHAT_FOOD_CHARS = 120

FOOD_LOG_PREFIX_RE = re.compile(r"^(?:记录饮食|饮食记录|吃了|记录吃了)\s*[:：]?\s*", re.IGNORECASE)
FOOD_AMOUNT_RE = re.compile(r"^(?P<name>.+?)\s*(?P<grams>\d+(?:\.\d+)?)\s*(?:克|g)$", re.IGNORECASE)
NUTRITION_SUMMARY_WORDS = frozenset({"今日营养", "今天营养", "营养汇总", "饮食进度", "今日饮食", "今天饮食"})

_NUTRIENT_IDS = {
    # FoodData Central API nutrient IDs.
    "1003": "protein_g",
    "1004": "fat_g",
    "1005": "carbs_g",
    "1008": "calories_kcal",
    "1079": "fiber_g",
    "1093": "sodium_mg",
    "2047": "calories_kcal",
    "2048": "calories_kcal",
    # Foundation Foods JSON uses legacy nutrient numbers.
    "203": "protein_g",
    "204": "fat_g",
    "205": "carbs_g",
    "208": "calories_kcal",
    "291": "fiber_g",
    "307": "sodium_mg",
}
_NUTRIENT_ALIASES = {
    "calories_kcal": (
        "calories_kcal",
        "calories",
        "energy_kcal",
        "energy-kcal_100g",
        "energy-kcal",
        "energy_kcal_100g",
    ),
    "protein_g": ("protein_g", "protein", "proteins", "proteins_100g", "protein_100g"),
    "fat_g": ("fat_g", "fat", "fat_100g", "total_fat", "total_fat_g"),
    "carbs_g": (
        "carbs_g",
        "carbs",
        "carbohydrates",
        "carbohydrates_100g",
        "carbohydrate_g",
    ),
    "fiber_g": ("fiber_g", "fiber", "fiber_100g", "dietary_fiber", "dietary_fiber_g"),
    "sodium_mg": ("sodium_mg", "sodium", "sodium_100g", "sodium_mg_100g"),
}


def _text(value: Any, *, name: str, limit: int, required: bool = False) -> str:
    result = sanitize(str(value or "")).strip()[:limit]
    if required and not result:
        raise ValueError(f"{name}不能为空")
    return result


def _items(value: Any, *, limit: int = MAX_LIST_ITEMS, item_limit: int = MAX_ALIAS_CHARS) -> list[str]:
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
        text = _text(item, name="别名", limit=item_limit)
        if text and text not in result:
            result.append(text)
    return result


def _number(value: Any, *, name: str, maximum: float) -> float | None:
    if isinstance(value, Mapping):
        value = value.get("value")
    if value in (None, ""):
        return None
    try:
        result = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}必须是数字") from exc
    if not math.isfinite(result) or result < 0 or result > maximum:
        raise ValueError(f"{name}必须在0到{maximum}之间")
    return round(result, 4)


def _nutrient_number(value: Any, *, name: str, maximum: float) -> float | None:
    """解析营养数值；对 USDA 的负数 by-difference 异常按 0 处理。"""
    candidate = value.get("value") if isinstance(value, Mapping) else value
    try:
        if candidate not in (None, "") and float(str(candidate).replace(",", "").strip()) < 0:
            return 0.0
    except (TypeError, ValueError):
        pass
    return _number(candidate, name=name, maximum=maximum)


def _first(record: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def _serving_size(record: Mapping[str, Any]) -> float | None:
    value = _first(record, "serving_size_g", "servingSize", "serving_size", "servingSizeG")
    if isinstance(value, str):
        match = re.search(r"(\d+(?:\.\d+)?)\s*g", value, flags=re.IGNORECASE)
        if match:
            value = match.group(1)
    return _number(value, name="每份克数", maximum=MAX_GRAMS)


def _food_nutrient_values(record: Mapping[str, Any]) -> tuple[dict[str, float], dict[str, float]]:
    """返回规范营养字段和额外数值字段。"""
    values: dict[str, float] = {}
    extras: dict[str, float] = {}

    def add(key: str, raw: Any, *, maximum: float = 100000.0) -> None:
        number = _nutrient_number(raw, name=key, maximum=maximum)
        if number is not None:
            extras[key[:80]] = number

    for key, aliases in _NUTRIENT_ALIASES.items():
        for alias in aliases:
            raw = record.get(alias)
            if raw not in (None, ""):
                maximum = 10000.0 if key == "calories_kcal" else (100000.0 if key == "sodium_mg" else 1000.0)
                parsed = _nutrient_number(raw, name=key, maximum=maximum)
                if parsed is not None:
                    values[key] = parsed
                    break

    nutriments = record.get("nutriments")
    if isinstance(nutriments, Mapping):
        for raw_key, raw_value in nutriments.items():
            key = str(raw_key).strip().casefold()
            if key.endswith("_serving") or key.endswith("_value"):
                continue
            add(key, raw_value)
            for target, aliases in _NUTRIENT_ALIASES.items():
                if key in aliases:
                    maximum = 10000.0 if target == "calories_kcal" else (100000.0 if target == "sodium_mg" else 1000.0)
                    parsed = _nutrient_number(raw_value, name=target, maximum=maximum)
                    if parsed is not None:
                        if target == "sodium_mg" and key in {"sodium_100g", "sodium_serving"}:
                            parsed = round(parsed * 1000, 4)
                        values[target] = parsed

    food_nutrients = record.get("foodNutrients")
    if isinstance(food_nutrients, Sequence) and not isinstance(food_nutrients, (str, bytes, bytearray)):
        for nutrient in food_nutrients[:MAX_NUTRIENTS]:
            if not isinstance(nutrient, Mapping):
                continue
            nested = nutrient.get("nutrient")
            nested = nested if isinstance(nested, Mapping) else {}
            identifier = str(
                nutrient.get("nutrientNumber")
                or nutrient.get("nutrientId")
                or nutrient.get("number")
                or nested.get("number")
                or nested.get("id")
                or nutrient.get("nutrientName")
                or nested.get("name")
                or ""
            ).strip().casefold()
            target = _NUTRIENT_IDS.get(identifier)
            nutrient_name = str(
                nutrient.get("nutrientName") or nested.get("name") or ""
            ).casefold()
            if not target:
                if "protein" in nutrient_name:
                    target = "protein_g"
                elif "carbohydrate" in nutrient_name:
                    target = "carbs_g"
                elif "fiber" in nutrient_name:
                    target = "fiber_g"
                elif "sodium" in nutrient_name:
                    target = "sodium_mg"
                elif "energy" in nutrient_name and "kj" not in nutrient_name:
                    target = "calories_kcal"
                elif "fat" in nutrient_name or "lipid" in nutrient_name:
                    target = "fat_g"
            raw_value = nutrient.get("value")
            if raw_value in (None, ""):
                raw_value = nutrient.get("amount")
            if raw_value in (None, ""):
                raw_value = nutrient.get("median")
            if target:
                maximum = 10000.0 if target == "calories_kcal" else (100000.0 if target == "sodium_mg" else 1000.0)
                parsed = _nutrient_number(raw_value, name=target, maximum=maximum)
                if parsed is not None:
                    values[target] = parsed
            if identifier:
                add(identifier, raw_value)

    label = record.get("labelNutrients")
    serving_size = _serving_size(record)
    if isinstance(label, Mapping) and serving_size and serving_size > 0:
        scale = 100.0 / serving_size
        label_aliases = {
            "calories": "calories_kcal",
            "protein": "protein_g",
            "fat": "fat_g",
            "carbohydrates": "carbs_g",
            "fiber": "fiber_g",
            "sodium": "sodium_mg",
        }
        for raw_key, target in label_aliases.items():
            if target not in values and raw_key in label:
                raw = label[raw_key]
                parsed = _nutrient_number(raw, name=target, maximum=100000.0)
                if parsed is not None:
                    values[target] = round(parsed * scale, 4)

    return values, extras


def normalize_food_record(
    record: Mapping[str, Any],
    *,
    source: str = "external",
    license_name: str = "",
    attribution: str = "",
) -> dict[str, Any]:
    """规范化 USDA FoodData Central 或 Open Food Facts 风格记录。"""
    if not isinstance(record, Mapping):
        raise ValueError("食品记录必须是对象")
    source_value = _text(record.get("source") or source, name="食品来源", limit=MAX_SOURCE_CHARS, required=True)
    source_id = _text(
        _first(record, "source_id", "fdcId", "id", "code", "barcode", "foodId", "slug", "name"),
        name="食品来源 ID",
        limit=MAX_SOURCE_ID_CHARS,
        required=True,
    )
    name = _text(
        _first(record, "name", "description", "product_name", "title", "foodName"),
        name="食品名称",
        limit=MAX_FOOD_NAME_CHARS,
        required=True,
    )
    brand = _first(record, "brand", "brandName", "brands")
    if isinstance(brand, Sequence) and not isinstance(brand, (str, bytes, bytearray)):
        brand = ", ".join(str(item) for item in brand[:8])
    barcode = _text(_first(record, "barcode", "code", "gtin"), name="条码", limit=MAX_BARCODE_CHARS)
    serving_size_g = _serving_size(record)
    values, extras = _food_nutrient_values(record)
    return {
        "source": source_value,
        "source_id": source_id,
        "name": name,
        "aliases": _items(record.get("aliases") or record.get("alias")),
        "brand": _text(brand, name="品牌", limit=MAX_BRAND_CHARS),
        "barcode": barcode,
        "serving_size_g": serving_size_g,
        "calories_kcal": values.get("calories_kcal", 0.0),
        "protein_g": values.get("protein_g", 0.0),
        "fat_g": values.get("fat_g", 0.0),
        "carbs_g": values.get("carbs_g", 0.0),
        "fiber_g": values.get("fiber_g", 0.0),
        "sodium_mg": values.get("sodium_mg", 0.0),
        "nutrients": extras,
        "license": _text(
            record.get("license") or record.get("license_name") or license_name,
            name="许可证",
            limit=MAX_LICENSE_CHARS,
        ),
        "attribution": _text(
            record.get("attribution") or attribution,
            name="署名",
            limit=MAX_ATTRIBUTION_CHARS,
        ),
    }


def _food_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key, default in (("aliases", []), ("nutrients", {})):
        try:
            result[key] = json.loads(result.get(key) or json.dumps(default))
        except (TypeError, json.JSONDecodeError):
            result[key] = default
    return result


def load_food_records(
    payload: Any,
    *,
    max_records: int | None = MAX_IMPORT_RECORDS,
) -> list[Mapping[str, Any]]:
    """读取 USDA/Open Food Facts 常见 JSON 顶层结构，不负责联网下载。"""
    if isinstance(payload, Mapping):
        for key in ("foods", "FoundationFoods", "products", "data", "results"):
            candidate = payload.get(key)
            if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes, bytearray)):
                payload = candidate
                break
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes, bytearray)):
        raise ValueError("食品 JSON 必须是数组或包含 foods/products/data/results 数组的对象")
    records = list(payload)
    if max_records is not None:
        max_records = int(max_records)
        if max_records < 1:
            raise ValueError("max_records 必须大于 0")
        if len(records) > max_records:
            raise ValueError(f"单次最多导入 {max_records} 条食品")
    if not all(isinstance(item, Mapping) for item in records):
        raise ValueError("食品数组中的每一项必须是对象")
    return records


def import_foods(
    records: Iterable[Mapping[str, Any]],
    *,
    source: str = "external",
    license_name: str = "",
    attribution: str = "",
    skip_invalid: bool = False,
) -> dict[str, Any]:
    raw_records = list(records)
    if len(raw_records) > MAX_IMPORT_RECORDS:
        raise ValueError(f"单次最多导入 {MAX_IMPORT_RECORDS} 条食品")
    normalized: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, record in enumerate(raw_records, 1):
        try:
            normalized.append(
                normalize_food_record(
                    record,
                    source=source,
                    license_name=license_name,
                    attribution=attribution,
                )
            )
        except ValueError as exc:
            if not skip_invalid:
                raise
            errors.append(f"第 {index} 条：{exc}")

    now = utc_iso()
    created = updated = 0
    with db_connection() as conn:
        for item in normalized:
            old = conn.execute(
                "SELECT id FROM fitness_foods WHERE source=? AND source_id=?",
                (item["source"], item["source_id"]),
            ).fetchone()
            values = (
                item["source"],
                item["source_id"],
                item["name"],
                json.dumps(item["aliases"], ensure_ascii=False, separators=(",", ":")),
                item["brand"],
                item["barcode"],
                item["serving_size_g"],
                item["calories_kcal"],
                item["protein_g"],
                item["fat_g"],
                item["carbs_g"],
                item["fiber_g"],
                item["sodium_mg"],
                json.dumps(item["nutrients"], ensure_ascii=False, separators=(",", ":")),
                item["license"],
                item["attribution"],
            )
            if old:
                conn.execute(
                    "UPDATE fitness_foods SET name=?, aliases=?, brand=?, barcode=?, serving_size_g=?, "
                    "calories_kcal=?, protein_g=?, fat_g=?, carbs_g=?, fiber_g=?, sodium_mg=?, nutrients=?, "
                    "license=?, attribution=?, updated_at=? WHERE id=?",
                    values[2:] + (now, old["id"]),
                )
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO fitness_foods(source, source_id, name, aliases, brand, barcode, serving_size_g, "
                    "calories_kcal, protein_g, fat_g, carbs_g, fiber_g, sodium_mg, nutrients, license, attribution, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values + (now, now),
                )
                created += 1
    result: dict[str, Any] = {
        "created": created,
        "updated": updated,
        "imported": len(normalized),
        "skipped": len(errors),
        "total": len(raw_records),
    }
    if errors:
        result["errors"] = errors[:20]
    return result


def _food_select() -> str:
    return (
        "SELECT id, source, source_id, name, aliases, brand, barcode, serving_size_g, calories_kcal, "
        "protein_g, fat_g, carbs_g, fiber_g, sodium_mg, nutrients, license, attribution, created_at, updated_at "
        "FROM fitness_foods"
    )


def list_foods(
    *,
    query: str = "",
    brand: str = "",
    source: str = "",
    limit: int = 20,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 100))
    clauses: list[str] = []
    args: list[Any] = []
    if query.strip():
        like = f"%{sanitize(query.strip())[:MAX_FOOD_NAME_CHARS]}%"
        clauses.append("(name LIKE ? OR aliases LIKE ? OR brand LIKE ? OR barcode LIKE ?)")
        args.extend([like, like, like, like])
    if brand.strip():
        clauses.append("brand LIKE ?")
        args.append(f"%{sanitize(brand.strip())[:MAX_BRAND_CHARS]}%")
    if source.strip():
        clauses.append("source=?")
        args.append(_text(source, name="食品来源", limit=MAX_SOURCE_CHARS))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with db_connection() as conn:
        rows = conn.execute(_food_select() + where + " ORDER BY name COLLATE NOCASE, id LIMIT ?", args + [limit]).fetchall()
    return [_food_payload(row) for row in rows]


def get_food(food_id: int) -> dict[str, Any] | None:
    with db_connection() as conn:
        row = conn.execute(_food_select() + " WHERE id=?", (int(food_id),)).fetchone()
    return _food_payload(row) if row else None


def _date(value: Any, *, default_today: bool = False) -> str:
    if value in (None, ""):
        if default_today:
            return datetime.now(timezone.utc).date().isoformat()
        return ""
    text = str(value).strip()[:MAX_DATE_CHARS]
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("日期必须是 YYYY-MM-DD") from exc
    return parsed.date().isoformat()


def _grams(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("食用克数必须是数字") from exc
    if not math.isfinite(result) or result <= 0 or result > MAX_GRAMS:
        raise ValueError(f"食用克数必须在0到{MAX_GRAMS}之间")
    return round(result, 2)


def parse_food_log_command(message: str) -> dict[str, Any] | None:
    """保守解析聊天饮食命令；命令必须明确写出食品名称和克数。"""
    text = str(message or "").strip()
    prefix = FOOD_LOG_PREFIX_RE.match(text)
    if not prefix:
        return None
    body = text[prefix.end():].strip()
    if not body:
        return {"error": "请写食品名称和克数，例如：记录饮食：燕麦 50g"}
    match = FOOD_AMOUNT_RE.fullmatch(body)
    if not match:
        return {"error": "饮食记录必须包含明确克数，例如：记录饮食：燕麦 50g；不按“一碗/一份”猜重量。"}
    food_name = sanitize(match.group("name")).strip(" ：:，,")[:MAX_CHAT_FOOD_CHARS]
    if not food_name:
        return {"error": "请提供食品名称。"}
    try:
        grams = _grams(match.group("grams"))
    except ValueError as exc:
        return {"error": str(exc)}
    return {"food_name": food_name, "grams": grams}


def calculate_nutrition(food: Mapping[str, Any], grams: Any) -> dict[str, Any]:
    grams_value = _grams(grams)
    factor = grams_value / 100.0
    return {
        "grams": grams_value,
        "calories_kcal": round(float(food.get("calories_kcal") or 0) * factor, 2),
        "protein_g": round(float(food.get("protein_g") or 0) * factor, 2),
        "fat_g": round(float(food.get("fat_g") or 0) * factor, 2),
        "carbs_g": round(float(food.get("carbs_g") or 0) * factor, 2),
        "fiber_g": round(float(food.get("fiber_g") or 0) * factor, 2),
        "sodium_mg": round(float(food.get("sodium_mg") or 0) * factor, 2),
    }


def _log_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return dict(row)


def log_food(
    user_id: str | None,
    food_id: int,
    *,
    grams: Any,
    meal: str = "",
    source: str = "local",
    external_id: str | None = None,
    eaten_at: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    from app.core.memory import normalize_user_id

    uid = normalize_user_id(user_id)
    grams_value = _grams(grams)
    source_value = _text(source or "local", name="记录来源", limit=MAX_SOURCE_CHARS, required=True)
    external_value = _text(external_id, name="外部 ID", limit=MAX_SOURCE_ID_CHARS) or None
    meal_value = _text(meal, name="餐次", limit=40)
    note_value = _text(note, name="备注", limit=500)
    eaten_value = _parse_datetime(eaten_at)
    now = utc_iso()
    with db_connection() as conn:
        food_row = conn.execute(_food_select() + " WHERE id=?", (int(food_id),)).fetchone()
        if not food_row:
            raise KeyError("食品不存在")
        food = _food_payload(food_row)
        nutrition = calculate_nutrition(food, grams_value)
        values = (
            uid,
            int(food_id),
            source_value,
            external_value,
            food["name"],
            food.get("brand", ""),
            nutrition["grams"],
            meal_value,
            nutrition["calories_kcal"],
            nutrition["protein_g"],
            nutrition["fat_g"],
            nutrition["carbs_g"],
            nutrition["fiber_g"],
            nutrition["sodium_mg"],
            note_value,
            eaten_value,
        )
        existing = None
        if external_value is not None:
            existing = conn.execute(
                "SELECT id FROM fitness_food_logs WHERE user_id=? AND source=? AND external_id=?",
                (uid, source_value, external_value),
            ).fetchone()
        if existing:
            conn.execute(
                "UPDATE fitness_food_logs SET food_id=?, food_name=?, brand=?, grams=?, meal=?, calories_kcal=?, "
                "protein_g=?, fat_g=?, carbs_g=?, fiber_g=?, sodium_mg=?, note=?, eaten_at=?, created_at=? WHERE id=?",
                (
                    values[1],
                    values[4],
                    values[5],
                    values[6],
                    values[7],
                    values[8],
                    values[9],
                    values[10],
                    values[11],
                    values[12],
                    values[13],
                    values[14],
                    values[15],
                    now,
                    existing["id"],
                ),
            )
            log_id = int(existing["id"])
        else:
            cur = conn.execute(
                "INSERT INTO fitness_food_logs(user_id, food_id, source, external_id, food_name, brand, grams, meal, "
                "calories_kcal, protein_g, fat_g, carbs_g, fiber_g, sodium_mg, note, eaten_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values + (now,),
            )
            log_id = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM fitness_food_logs WHERE id=? AND user_id=?", (log_id, uid)).fetchone()
    return _log_payload(row)


def _parse_datetime(value: Any) -> str:
    if value in (None, ""):
        return utc_iso()
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("记录时间必须是 ISO 时间") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def list_food_logs(
    user_id: str | None,
    *,
    date: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    from app.core.memory import normalize_user_id

    uid = normalize_user_id(user_id)
    date_value = _date(date) if date else ""
    limit = max(1, min(int(limit), 200))
    clauses = ["user_id=?"]
    args: list[Any] = [uid]
    if date_value:
        clauses.append("substr(eaten_at, 1, 10)=?")
        args.append(date_value)
    args.append(limit)
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT id, user_id, food_id, source, external_id, food_name, brand, grams, meal, calories_kcal, "
            "protein_g, fat_g, carbs_g, fiber_g, sodium_mg, note, eaten_at, created_at "
            f"FROM fitness_food_logs WHERE {' AND '.join(clauses)} ORDER BY eaten_at DESC, id DESC LIMIT ?",
            args,
        ).fetchall()
    return [_log_payload(row) for row in rows]


def nutrition_summary(user_id: str | None, *, date: str | None = None) -> dict[str, Any]:
    from app.core.memory import normalize_user_id

    uid = normalize_user_id(user_id)
    date_value = _date(date, default_today=True)
    with db_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS log_count, COALESCE(SUM(calories_kcal), 0) AS calories_kcal, "
            "COALESCE(SUM(protein_g), 0) AS protein_g, COALESCE(SUM(fat_g), 0) AS fat_g, "
            "COALESCE(SUM(carbs_g), 0) AS carbs_g, COALESCE(SUM(fiber_g), 0) AS fiber_g, "
            "COALESCE(SUM(sodium_mg), 0) AS sodium_mg FROM fitness_food_logs "
            "WHERE user_id=? AND substr(eaten_at, 1, 10)=?",
            (uid, date_value),
        ).fetchone()
    result = dict(row)
    for key in ("calories_kcal", "protein_g", "fat_g", "carbs_g", "fiber_g", "sodium_mg"):
        result[key] = round(float(result.get(key) or 0), 2)
    result["date"] = date_value
    return result


def _display_number(value: Any) -> str:
    try:
        return f"{float(value or 0):g}"
    except (TypeError, ValueError):
        return "0"


def format_food_log_reply(food: Mapping[str, Any], nutrition: Mapping[str, Any]) -> str:
    """格式化一次饮食记录的确定性回复，不引入模型猜测。"""
    return (
        f"🍽️ 已记录：{food.get('name', '食品')} {nutrition.get('grams', 0):g}g ✓\n"
        f"热量 {_display_number(nutrition.get('calories_kcal'))} kcal，"
        f"蛋白质 {_display_number(nutrition.get('protein_g'))}g，"
        f"脂肪 {_display_number(nutrition.get('fat_g'))}g，"
        f"碳水 {_display_number(nutrition.get('carbs_g'))}g"
    )


def format_nutrition_summary(summary: Mapping[str, Any], logs: Sequence[Mapping[str, Any]] = ()) -> str:
    """格式化某日饮食汇总和最近记录。"""
    lines = [
        f"🍽️ {summary.get('date', '今日')} 饮食汇总："
        f"{_display_number(summary.get('calories_kcal'))} kcal，"
        f"蛋白质 {_display_number(summary.get('protein_g'))}g，"
        f"脂肪 {_display_number(summary.get('fat_g'))}g，"
        f"碳水 {_display_number(summary.get('carbs_g'))}g，"
        f"纤维 {_display_number(summary.get('fiber_g'))}g",
    ]
    if not logs:
        lines.append("今天还没有饮食记录。")
        return "\\n".join(lines)
    lines.append("最近记录：")
    for log in list(logs)[:10]:
        meal = f"{log.get('meal')}：" if log.get("meal") else ""
        lines.append(
            f"- {meal}{log.get('food_name', '食品')} {log.get('grams', 0):g}g，"
            f"{_display_number(log.get('calories_kcal'))} kcal"
        )
    return "\\n".join(lines)


def delete_food_log(user_id: str | None, log_id: int) -> None:
    from app.core.memory import normalize_user_id

    uid = normalize_user_id(user_id)
    with db_connection() as conn:
        cur = conn.execute("DELETE FROM fitness_food_logs WHERE id=? AND user_id=?", (int(log_id), uid))
        if not cur.rowcount:
            raise KeyError("饮食记录不存在")
