"""健身领域 SQLite 仓储。

本模块是健身结构化数据的唯一 SQL 边界。输入规范化、聊天解析、营养计算和
统计展示仍由上层服务负责；仓储只负责参数化 SQL、事务、联表结果和 JSON
字段编解码。连接统一复用 ``app.models.database.db_connection``。
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from app.common.timeutil import utc_iso
from app.models.database import db_connection


class FitnessRepository(Protocol):
    """健身服务依赖的持久化契约，测试可注入内存替身。"""

    def exercise_exists(self, exercise_id: int) -> bool: ...
    def upsert_exercises(self, records: Sequence[Mapping[str, Any]], *, now: str) -> dict[str, int]: ...
    def has_builtin_exercises(self) -> bool: ...
    def list_exercises(self, *, query: str = "", muscle: str = "", equipment: str = "", limit: int = 20) -> list[dict[str, Any]]: ...
    def get_exercise(self, exercise_id: int) -> dict[str, Any] | None: ...
    def record_import(self, user_id: str, *, source: str, external_id: str | None, content_hash: str, status: str, imported_count: int) -> dict[str, Any]: ...

    def upsert_foods(self, records: Sequence[Mapping[str, Any]], *, now: str) -> dict[str, Any]: ...
    def list_foods(self, *, query: str = "", brand: str = "", source: str = "", limit: int = 20) -> list[dict[str, Any]]: ...
    def get_food(self, food_id: int) -> dict[str, Any] | None: ...
    def upsert_food_log(self, user_id: str, food_id: int, *, source: str, external_id: str | None, food: Mapping[str, Any], nutrition: Mapping[str, Any], meal: str, note: str, eaten_at: str, now: str) -> dict[str, Any]: ...
    def list_food_logs(self, user_id: str, *, date: str | None = None, limit: int = 50) -> list[dict[str, Any]]: ...
    def nutrition_summary(self, user_id: str, *, date: str) -> dict[str, Any]: ...
    def delete_food_log(self, user_id: str, log_id: int) -> None: ...

    def upsert_profile(self, user_id: str, *, goal: str, experience: str, sessions_per_week: int | None, session_minutes: int | None, equipment: str, limitations: str, notes: str, now: str) -> dict[str, Any]: ...
    def get_profile(self, user_id: str) -> dict[str, Any] | None: ...
    def create_plan(self, user_id: str, *, name: str, goal: str, source: str, notes: str, days: Sequence[Mapping[str, Any]], now: str) -> dict[str, Any] | None: ...
    def list_plans(self, user_id: str, *, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]: ...
    def get_plan(self, user_id: str, plan_id: int) -> dict[str, Any] | None: ...
    def activate_plan(self, user_id: str, plan_id: int, *, now: str) -> dict[str, Any] | None: ...
    def archive_plan(self, user_id: str, plan_id: int, *, now: str) -> dict[str, Any] | None: ...
    def start_session(self, user_id: str, *, plan_id: int | None, plan_day_id: int | None, source: str, external_id: str | None, notes: str, now: str) -> dict[str, Any] | None: ...
    def get_session(self, user_id: str, session_id: int) -> dict[str, Any] | None: ...
    def list_sessions(self, user_id: str, *, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]: ...
    def log_set(self, user_id: str, session_id: int, exercise_id: int, *, reps: int, weight_kg: float, set_order: int | None, set_type: str, rpe: float | None, rir: float | None, rest_seconds: int | None, is_warmup: bool, note: str, now: str) -> dict[str, Any]: ...
    def complete_session(self, user_id: str, session_id: int, *, now: str) -> dict[str, Any] | None: ...
    def cancel_session(self, user_id: str, session_id: int, *, now: str) -> dict[str, Any] | None: ...
    def record_measurement(self, user_id: str, *, kind: str, value: float, unit: str, note: str, measured_at: str, now: str) -> dict[str, Any] | None: ...
    def list_measurements(self, user_id: str, *, kind: str | None = None, limit: int = 30) -> list[dict[str, Any]]: ...
    def summary_rows(self, user_id: str, *, since: str) -> dict[str, list[dict[str, Any]]]: ...
    def latest_legacy_logs(self, user_id: str, *, limit: int = 5) -> list[dict[str, Any]]: ...
    def get_active_plan(self, user_id: str) -> dict[str, Any] | None: ...
    def latest_in_progress_session(self, user_id: str) -> dict[str, Any] | None: ...


def _json_load(value: Any, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def _list_json(value: Any) -> list[Any]:
    result = _json_load(value, [])
    return result if isinstance(result, list) else []


def _dict_json(value: Any) -> dict[str, Any]:
    result = _json_load(value, {})
    return result if isinstance(result, dict) else {}


class SQLiteFitnessRepository:
    """基于现有 SQLite 连接工厂的健身领域仓储。"""

    @staticmethod
    def _exercise_payload(row: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(row)
        for key in ("aliases", "primary_muscles", "secondary_muscles", "instructions", "images"):
            result[key] = _list_json(result.get(key))
        return result

    @staticmethod
    def _food_select() -> str:
        return (
            "SELECT id, source, source_id, name, aliases, brand, barcode, serving_size_g, calories_kcal, "
            "protein_g, fat_g, carbs_g, fiber_g, sodium_mg, nutrients, license, attribution, created_at, updated_at "
            "FROM fitness_foods"
        )

    @classmethod
    def _food_payload(cls, row: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["aliases"] = _list_json(result.get("aliases"))
        result["nutrients"] = _dict_json(result.get("nutrients"))
        return result

    @staticmethod
    def _plan_payload(conn, plan_id: int, user_id: str) -> dict[str, Any] | None:
        plan = conn.execute(
            "SELECT id, user_id, name, goal, status, source, notes, version, created_at, updated_at "
            "FROM fitness_plans WHERE id=? AND user_id=?",
            (plan_id, user_id),
        ).fetchone()
        if not plan:
            return None
        result = dict(plan)
        result["days"] = []
        days = conn.execute(
            "SELECT id, day_index, name, notes FROM fitness_plan_days WHERE plan_id=? ORDER BY day_index, id",
            (plan_id,),
        ).fetchall()
        for day in days:
            day_payload = dict(day)
            exercises = conn.execute(
                "SELECT pe.id, pe.exercise_id, pe.sort_order, pe.sets, pe.rep_min, pe.rep_max, "
                "pe.rir_target, pe.rest_seconds, pe.notes, e.name, e.primary_muscles, e.equipment "
                "FROM fitness_plan_exercises pe JOIN fitness_exercises e ON e.id=pe.exercise_id "
                "WHERE pe.plan_day_id=? ORDER BY pe.sort_order, pe.id",
                (day["id"],),
            ).fetchall()
            day_payload["exercises"] = []
            for exercise in exercises:
                item = dict(exercise)
                item["primary_muscles"] = _list_json(item.get("primary_muscles"))
                day_payload["exercises"].append(item)
            result["days"].append(day_payload)
        return result

    @staticmethod
    def _session_payload(conn, session_id: int, user_id: str) -> dict[str, Any] | None:
        session = conn.execute(
            "SELECT s.id, s.user_id, s.plan_id, s.plan_day_id, s.status, s.source, s.external_id, "
            "s.notes, s.started_at, s.completed_at, p.name AS plan_name, d.name AS day_name "
            "FROM fitness_sessions s LEFT JOIN fitness_plans p ON p.id=s.plan_id "
            "LEFT JOIN fitness_plan_days d ON d.id=s.plan_day_id "
            "WHERE s.id=? AND s.user_id=?",
            (session_id, user_id),
        ).fetchone()
        if not session:
            return None
        result = dict(session)
        sets = conn.execute(
            "SELECT fs.id, fs.exercise_id, fs.set_order, fs.set_type, fs.reps, fs.weight_kg, fs.rpe, "
            "fs.rir, fs.rest_seconds, fs.is_warmup, fs.note, fs.created_at, e.name AS exercise_name "
            "FROM fitness_sets fs JOIN fitness_exercises e ON e.id=fs.exercise_id "
            "WHERE fs.session_id=? ORDER BY fs.exercise_id, fs.set_order, fs.id",
            (session_id,),
        ).fetchall()
        result["sets"] = [dict(row) for row in sets]
        result["set_count"] = len(result["sets"])
        result["volume_kg"] = round(
            sum((row["reps"] or 0) * (row["weight_kg"] or 0) for row in sets if not row["is_warmup"]),
            2,
        )
        return result

    # ── 动作目录 ─────────────────────────────────────────────

    def exercise_exists(self, exercise_id: int) -> bool:
        with db_connection() as conn:
            return bool(conn.execute("SELECT 1 FROM fitness_exercises WHERE id=?", (exercise_id,)).fetchone())

    def upsert_exercises(self, records: Sequence[Mapping[str, Any]], *, now: str) -> dict[str, int]:
        created = updated = 0
        with db_connection() as conn:
            for item in records:
                old = conn.execute(
                    "SELECT id FROM fitness_exercises WHERE source=? AND source_id=?",
                    (item["source"], item["source_id"]),
                ).fetchone()
                values = (
                    item["source"], item["source_id"], item["name"],
                    json.dumps(item["aliases"], ensure_ascii=False, separators=(",", ":")),
                    json.dumps(item["primary_muscles"], ensure_ascii=False, separators=(",", ":")),
                    json.dumps(item["secondary_muscles"], ensure_ascii=False, separators=(",", ":")),
                    item["equipment"],
                    json.dumps(item["instructions"], ensure_ascii=False, separators=(",", ":")),
                    json.dumps(item["images"], ensure_ascii=False, separators=(",", ":")),
                    item["license"], item["attribution"],
                )
                if old:
                    conn.execute(
                        "UPDATE fitness_exercises SET name=?, aliases=?, primary_muscles=?, secondary_muscles=?, "
                        "equipment=?, instructions=?, images=?, license=?, attribution=?, updated_at=? WHERE id=?",
                        values[2:] + (now, old["id"]),
                    )
                    updated += 1
                else:
                    conn.execute(
                        "INSERT INTO fitness_exercises "
                        "(source, source_id, name, aliases, primary_muscles, secondary_muscles, equipment, "
                        "instructions, images, license, attribution, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        values + (now, now),
                    )
                    created += 1
        return {"created": created, "updated": updated, "total": len(records)}

    def has_builtin_exercises(self) -> bool:
        with db_connection() as conn:
            return bool(conn.execute("SELECT 1 FROM fitness_exercises WHERE source='builtin' LIMIT 1").fetchone())

    def list_exercises(self, *, query: str = "", muscle: str = "", equipment: str = "", limit: int = 20) -> list[dict[str, Any]]:
        terms = [query.strip(), muscle.strip(), equipment.strip()]
        clauses: list[str] = []
        args: list[Any] = []
        if terms[0]:
            like = f"%{terms[0]}%"
            clauses.append("(name LIKE ? OR aliases LIKE ? OR primary_muscles LIKE ? OR secondary_muscles LIKE ?)")
            args.extend([like, like, like, like])
        if terms[1]:
            like = f"%{terms[1]}%"
            clauses.append("(primary_muscles LIKE ? OR secondary_muscles LIKE ?)")
            args.extend([like, like])
        if terms[2]:
            clauses.append("equipment LIKE ?")
            args.append(f"%{terms[2]}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with db_connection() as conn:
            rows = conn.execute(
                "SELECT id, source, source_id, name, aliases, primary_muscles, secondary_muscles, equipment, "
                "instructions, images, license, attribution, created_at, updated_at "
                f"FROM fitness_exercises {where} ORDER BY name COLLATE NOCASE, id LIMIT ?",
                args + [limit],
            ).fetchall()
        return [self._exercise_payload(row) for row in rows]

    def get_exercise(self, exercise_id: int) -> dict[str, Any] | None:
        with db_connection() as conn:
            row = conn.execute(
                "SELECT id, source, source_id, name, aliases, primary_muscles, secondary_muscles, equipment, "
                "instructions, images, license, attribution, created_at, updated_at "
                "FROM fitness_exercises WHERE id=?",
                (int(exercise_id),),
            ).fetchone()
        return self._exercise_payload(row) if row else None

    def record_import(self, user_id: str, *, source: str, external_id: str | None, content_hash: str, status: str, imported_count: int) -> dict[str, Any]:
        now = utc_iso()
        with db_connection() as conn:
            conn.execute(
                "INSERT INTO fitness_imports(user_id, source, external_id, content_hash, status, imported_count, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, source, external_id, content_hash) DO UPDATE SET status=excluded.status, "
                "imported_count=excluded.imported_count, updated_at=excluded.updated_at",
                (user_id, source, external_id, content_hash, status, imported_count, now, now),
            )
            row = conn.execute(
                "SELECT id, user_id, source, external_id, content_hash, status, imported_count, created_at, updated_at "
                "FROM fitness_imports WHERE user_id=? AND source=? AND external_id IS ? AND content_hash=?",
                (user_id, source, external_id, content_hash),
            ).fetchone()
        return dict(row)

    # ── 食品与饮食记录 ───────────────────────────────────────

    def upsert_foods(self, records: Sequence[Mapping[str, Any]], *, now: str) -> dict[str, Any]:
        created = updated = 0
        with db_connection() as conn:
            for item in records:
                old = conn.execute(
                    "SELECT id FROM fitness_foods WHERE source=? AND source_id=?",
                    (item["source"], item["source_id"]),
                ).fetchone()
                values = (
                    item["source"], item["source_id"], item["name"],
                    json.dumps(item["aliases"], ensure_ascii=False, separators=(",", ":")),
                    item["brand"], item["barcode"], item["serving_size_g"],
                    item["calories_kcal"], item["protein_g"], item["fat_g"], item["carbs_g"],
                    item["fiber_g"], item["sodium_mg"],
                    json.dumps(item["nutrients"], ensure_ascii=False, separators=(",", ":")),
                    item["license"], item["attribution"],
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
        return {"created": created, "updated": updated, "imported": len(records), "skipped": 0, "total": len(records)}

    def list_foods(self, *, query: str = "", brand: str = "", source: str = "", limit: int = 20) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if query.strip():
            like = f"%{query.strip()}%"
            clauses.append("(name LIKE ? OR aliases LIKE ? OR brand LIKE ? OR barcode LIKE ?)")
            args.extend([like, like, like, like])
        if brand.strip():
            clauses.append("brand LIKE ?")
            args.append(f"%{brand.strip()}%")
        if source.strip():
            clauses.append("source=?")
            args.append(source.strip())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with db_connection() as conn:
            rows = conn.execute(self._food_select() + where + " ORDER BY name COLLATE NOCASE, id LIMIT ?", args + [limit]).fetchall()
        return [self._food_payload(row) for row in rows]

    def get_food(self, food_id: int) -> dict[str, Any] | None:
        with db_connection() as conn:
            row = conn.execute(self._food_select() + " WHERE id=?", (int(food_id),)).fetchone()
        return self._food_payload(row) if row else None

    def upsert_food_log(self, user_id: str, food_id: int, *, source: str, external_id: str | None, food: Mapping[str, Any], nutrition: Mapping[str, Any], meal: str, note: str, eaten_at: str, now: str) -> dict[str, Any]:
        with db_connection() as conn:
            if not conn.execute("SELECT 1 FROM fitness_foods WHERE id=?", (int(food_id),)).fetchone():
                raise KeyError("食品不存在")
            values = (
                user_id, int(food_id), source, external_id, food.get("name", ""), food.get("brand", ""),
                nutrition["grams"], meal, nutrition["calories_kcal"], nutrition["protein_g"],
                nutrition["fat_g"], nutrition["carbs_g"], nutrition["fiber_g"], nutrition["sodium_mg"],
                note, eaten_at,
            )
            existing = None
            if external_id is not None:
                existing = conn.execute(
                    "SELECT id FROM fitness_food_logs WHERE user_id=? AND source=? AND external_id=?",
                    (user_id, source, external_id),
                ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE fitness_food_logs SET food_id=?, food_name=?, brand=?, grams=?, meal=?, calories_kcal=?, "
                    "protein_g=?, fat_g=?, carbs_g=?, fiber_g=?, sodium_mg=?, note=?, eaten_at=?, created_at=? WHERE id=?",
                    (
                        values[1], values[4], values[5], values[6], values[7], values[8],
                        values[9], values[10], values[11], values[12], values[13], values[14],
                        values[15], now, existing["id"],
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
            row = conn.execute("SELECT * FROM fitness_food_logs WHERE id=? AND user_id=?", (log_id, user_id)).fetchone()
        return dict(row)

    def list_food_logs(self, user_id: str, *, date: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        clauses = ["user_id=?"]
        args: list[Any] = [user_id]
        if date:
            clauses.append("substr(eaten_at, 1, 10)=?")
            args.append(date)
        args.append(limit)
        with db_connection() as conn:
            rows = conn.execute(
                "SELECT id, user_id, food_id, source, external_id, food_name, brand, grams, meal, calories_kcal, "
                "protein_g, fat_g, carbs_g, fiber_g, sodium_mg, note, eaten_at, created_at "
                f"FROM fitness_food_logs WHERE {' AND '.join(clauses)} ORDER BY eaten_at DESC, id DESC LIMIT ?",
                args,
            ).fetchall()
        return [dict(row) for row in rows]

    def nutrition_summary(self, user_id: str, *, date: str) -> dict[str, Any]:
        with db_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS log_count, COALESCE(SUM(calories_kcal), 0) AS calories_kcal, "
                "COALESCE(SUM(protein_g), 0) AS protein_g, COALESCE(SUM(fat_g), 0) AS fat_g, "
                "COALESCE(SUM(carbs_g), 0) AS carbs_g, COALESCE(SUM(fiber_g), 0) AS fiber_g, "
                "COALESCE(SUM(sodium_mg), 0) AS sodium_mg FROM fitness_food_logs "
                "WHERE user_id=? AND substr(eaten_at, 1, 10)=?",
                (user_id, date),
            ).fetchone()
        return dict(row)

    def delete_food_log(self, user_id: str, log_id: int) -> None:
        with db_connection() as conn:
            cur = conn.execute("DELETE FROM fitness_food_logs WHERE id=? AND user_id=?", (int(log_id), user_id))
            if not cur.rowcount:
                raise KeyError("饮食记录不存在")

    # ── 训练计划、会话与指标 ─────────────────────────────────

    def upsert_profile(self, user_id: str, *, goal: str, experience: str, sessions_per_week: int | None, session_minutes: int | None, equipment: str, limitations: str, notes: str, now: str) -> dict[str, Any]:
        with db_connection() as conn:
            conn.execute(
                "INSERT INTO fitness_profile(user_id, goal, experience, sessions_per_week, session_minutes, equipment, "
                "limitations, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET goal=excluded.goal, experience=excluded.experience, "
                "sessions_per_week=excluded.sessions_per_week, session_minutes=excluded.session_minutes, "
                "equipment=excluded.equipment, limitations=excluded.limitations, notes=excluded.notes, updated_at=excluded.updated_at",
                (user_id, goal, experience, sessions_per_week, session_minutes, equipment, limitations, notes, now, now),
            )
            row = conn.execute("SELECT * FROM fitness_profile WHERE user_id=?", (user_id,)).fetchone()
        result = dict(row)
        result["equipment"] = _list_json(result.get("equipment"))
        return result

    def get_profile(self, user_id: str) -> dict[str, Any] | None:
        with db_connection() as conn:
            row = conn.execute("SELECT * FROM fitness_profile WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["equipment"] = _list_json(result.get("equipment"))
        return result

    def create_plan(self, user_id: str, *, name: str, goal: str, source: str, notes: str, days: Sequence[Mapping[str, Any]], now: str) -> dict[str, Any] | None:
        with db_connection() as conn:
            for day in days:
                for item in day["exercises"]:
                    if not self._exercise_exists_conn(conn, int(item["exercise_id"])):
                        raise ValueError(f"动作不存在：{item['exercise_id']}")
            cur = conn.execute(
                "INSERT INTO fitness_plans(user_id, name, goal, status, source, notes, version, created_at, updated_at) "
                "VALUES (?, ?, ?, 'draft', ?, ?, 1, ?, ?)",
                (user_id, name, goal, source, notes, now, now),
            )
            plan_id = int(cur.lastrowid)
            for day in days:
                day_cur = conn.execute(
                    "INSERT INTO fitness_plan_days(plan_id, day_index, name, notes) VALUES (?, ?, ?, ?)",
                    (plan_id, day["day_index"], day["name"], day["notes"]),
                )
                day_id = int(day_cur.lastrowid)
                for item in day["exercises"]:
                    conn.execute(
                        "INSERT INTO fitness_plan_exercises(plan_day_id, exercise_id, sort_order, sets, rep_min, rep_max, "
                        "rir_target, rest_seconds, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (day_id, item["exercise_id"], item["sort_order"], item["sets"], item["rep_min"], item["rep_max"], item["rir_target"], item["rest_seconds"], item["notes"]),
                    )
            return self._plan_payload(conn, plan_id, user_id)

    @staticmethod
    def _exercise_exists_conn(conn, exercise_id: int) -> bool:
        return bool(conn.execute("SELECT 1 FROM fitness_exercises WHERE id=?", (exercise_id,)).fetchone())

    def list_plans(self, user_id: str, *, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        clauses = ["user_id=?"]
        args: list[Any] = [user_id]
        if status:
            clauses.append("status=?")
            args.append(status)
        args.append(limit)
        with db_connection() as conn:
            rows = conn.execute(
                "SELECT id, user_id, name, goal, status, source, notes, version, created_at, updated_at "
                f"FROM fitness_plans WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC, id DESC LIMIT ?",
                args,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_plan(self, user_id: str, plan_id: int) -> dict[str, Any] | None:
        with db_connection() as conn:
            return self._plan_payload(conn, int(plan_id), user_id)

    def activate_plan(self, user_id: str, plan_id: int, *, now: str) -> dict[str, Any] | None:
        with db_connection() as conn:
            if not conn.execute("SELECT id FROM fitness_plans WHERE id=? AND user_id=?", (int(plan_id), user_id)).fetchone():
                raise KeyError("计划不存在")
            conn.execute(
                "UPDATE fitness_plans SET status='archived', version=version+1, updated_at=? WHERE user_id=? AND status='active' AND id<>?",
                (now, user_id, int(plan_id)),
            )
            conn.execute(
                "UPDATE fitness_plans SET status='active', version=version+1, updated_at=? WHERE id=? AND user_id=?",
                (now, int(plan_id), user_id),
            )
            return self._plan_payload(conn, int(plan_id), user_id)

    def archive_plan(self, user_id: str, plan_id: int, *, now: str) -> dict[str, Any] | None:
        with db_connection() as conn:
            cur = conn.execute(
                "UPDATE fitness_plans SET status='archived', version=version+1, updated_at=? "
                "WHERE id=? AND user_id=? AND status<>'archived'",
                (now, int(plan_id), user_id),
            )
            if not cur.rowcount:
                raise KeyError("计划不存在")
            return self._plan_payload(conn, int(plan_id), user_id)

    def start_session(self, user_id: str, *, plan_id: int | None, plan_day_id: int | None, source: str, external_id: str | None, notes: str, now: str) -> dict[str, Any] | None:
        with db_connection() as conn:
            if external_id:
                existing = conn.execute(
                    "SELECT id FROM fitness_sessions WHERE user_id=? AND source=? AND external_id=?",
                    (user_id, source, external_id),
                ).fetchone()
                if existing:
                    return self._session_payload(conn, int(existing["id"]), user_id)
            if plan_id is not None and not conn.execute("SELECT id FROM fitness_plans WHERE id=? AND user_id=?", (int(plan_id), user_id)).fetchone():
                raise KeyError("计划不存在")
            if plan_day_id is not None:
                day = conn.execute(
                    "SELECT d.id, d.plan_id FROM fitness_plan_days d JOIN fitness_plans p ON p.id=d.plan_id WHERE d.id=? AND p.user_id=?",
                    (int(plan_day_id), user_id),
                ).fetchone()
                if not day:
                    raise KeyError("训练日不存在")
                if plan_id is not None and int(day["plan_id"]) != int(plan_id):
                    raise ValueError("训练日不属于指定计划")
                plan_id = int(day["plan_id"])
            cur = conn.execute(
                "INSERT INTO fitness_sessions(user_id, plan_id, plan_day_id, status, source, external_id, notes, started_at) "
                "VALUES (?, ?, ?, 'in_progress', ?, ?, ?, ?)",
                (user_id, plan_id, plan_day_id, source, external_id, notes, now),
            )
            return self._session_payload(conn, int(cur.lastrowid), user_id)

    def get_session(self, user_id: str, session_id: int) -> dict[str, Any] | None:
        with db_connection() as conn:
            return self._session_payload(conn, int(session_id), user_id)

    def list_sessions(self, user_id: str, *, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        clauses = ["user_id=?"]
        args: list[Any] = [user_id]
        if status:
            clauses.append("status=?")
            args.append(status)
        args.append(limit)
        with db_connection() as conn:
            rows = conn.execute(
                "SELECT id FROM fitness_sessions "
                f"WHERE {' AND '.join(clauses)} ORDER BY started_at DESC, id DESC LIMIT ?",
                args,
            ).fetchall()
            return [self._session_payload(conn, int(row["id"]), user_id) or {} for row in rows]

    def log_set(self, user_id: str, session_id: int, exercise_id: int, *, reps: int, weight_kg: float, set_order: int | None, set_type: str, rpe: float | None, rir: float | None, rest_seconds: int | None, is_warmup: bool, note: str, now: str) -> dict[str, Any]:
        with db_connection() as conn:
            session = conn.execute("SELECT id, status FROM fitness_sessions WHERE id=? AND user_id=?", (int(session_id), user_id)).fetchone()
            if not session:
                raise KeyError("训练会话不存在")
            if session["status"] != "in_progress":
                raise ValueError("只有进行中的训练会话可以记录训练组")
            if not self._exercise_exists_conn(conn, int(exercise_id)):
                raise KeyError("动作不存在")
            if set_order is None:
                row = conn.execute(
                    "SELECT COALESCE(MAX(set_order), 0) + 1 AS next_order FROM fitness_sets WHERE session_id=? AND exercise_id=?",
                    (int(session_id), int(exercise_id)),
                ).fetchone()
                set_order = int(row["next_order"])
            cur = conn.execute(
                "INSERT INTO fitness_sets(session_id, exercise_id, set_order, set_type, reps, weight_kg, rpe, rir, rest_seconds, is_warmup, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (int(session_id), int(exercise_id), set_order, set_type, reps, weight_kg, rpe, rir, rest_seconds, int(is_warmup), note, now),
            )
            row = conn.execute(
                "SELECT fs.id, fs.session_id, fs.exercise_id, fs.set_order, fs.set_type, fs.reps, fs.weight_kg, "
                "fs.rpe, fs.rir, fs.rest_seconds, fs.is_warmup, fs.note, fs.created_at, e.name AS exercise_name "
                "FROM fitness_sets fs JOIN fitness_exercises e ON e.id=fs.exercise_id WHERE fs.id=?",
                (int(cur.lastrowid),),
            ).fetchone()
        return dict(row)

    def complete_session(self, user_id: str, session_id: int, *, now: str) -> dict[str, Any] | None:
        with db_connection() as conn:
            session = conn.execute("SELECT status FROM fitness_sessions WHERE id=? AND user_id=?", (int(session_id), user_id)).fetchone()
            if not session:
                raise KeyError("训练会话不存在")
            if session["status"] == "cancelled":
                raise ValueError("已取消的训练会话不能完成")
            if session["status"] == "in_progress":
                conn.execute("UPDATE fitness_sessions SET status='completed', completed_at=? WHERE id=? AND user_id=?", (now, int(session_id), user_id))
            return self._session_payload(conn, int(session_id), user_id)

    def cancel_session(self, user_id: str, session_id: int, *, now: str) -> dict[str, Any] | None:
        with db_connection() as conn:
            cur = conn.execute(
                "UPDATE fitness_sessions SET status='cancelled', completed_at=COALESCE(completed_at, ?) "
                "WHERE id=? AND user_id=? AND status='in_progress'",
                (now, int(session_id), user_id),
            )
            if not cur.rowcount:
                raise KeyError("进行中的训练会话不存在")
            return self._session_payload(conn, int(session_id), user_id)

    def record_measurement(self, user_id: str, *, kind: str, value: float, unit: str, note: str, measured_at: str, now: str) -> dict[str, Any] | None:
        with db_connection() as conn:
            cur = conn.execute(
                "INSERT INTO fitness_measurements(user_id, kind, value, unit, note, measured_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, kind, value, unit, note, measured_at, now),
            )
            if kind == "weight" and unit.casefold() in {"kg", "公斤", "千克"}:
                conn.execute(
                    "INSERT INTO fitness_log(user_id, kind, value, detail, created_at) VALUES (?, 'weight', ?, '', ?)",
                    (user_id, value, measured_at),
                )
            row = conn.execute(
                "SELECT id, user_id, kind, value, unit, note, measured_at, created_at FROM fitness_measurements WHERE id=?",
                (int(cur.lastrowid),),
            ).fetchone()
        return dict(row)

    def list_measurements(self, user_id: str, *, kind: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        clauses = ["user_id=?"]
        args: list[Any] = [user_id]
        if kind:
            clauses.append("kind=?")
            args.append(kind)
        args.append(limit)
        with db_connection() as conn:
            rows = conn.execute(
                "SELECT id, user_id, kind, value, unit, note, measured_at, created_at "
                f"FROM fitness_measurements WHERE {' AND '.join(clauses)} ORDER BY measured_at DESC, id DESC LIMIT ?",
                args,
            ).fetchall()
        return [dict(row) for row in rows]

    def summary_rows(self, user_id: str, *, since: str) -> dict[str, list[dict[str, Any]]]:
        with db_connection() as conn:
            sessions = conn.execute(
                "SELECT id, status, started_at, completed_at FROM fitness_sessions WHERE user_id=? AND started_at>=? AND status<>'cancelled' ORDER BY started_at DESC, id DESC",
                (user_id, since),
            ).fetchall()
            set_rows = conn.execute(
                "SELECT fs.exercise_id, fs.reps, fs.weight_kg, fs.rpe, fs.rir, fs.is_warmup, fs.created_at, e.name, e.primary_muscles "
                "FROM fitness_sets fs JOIN fitness_sessions s ON s.id=fs.session_id JOIN fitness_exercises e ON e.id=fs.exercise_id "
                "WHERE s.user_id=? AND s.started_at>=? AND s.status<>'cancelled'",
                (user_id, since),
            ).fetchall()
            measurements = conn.execute(
                "SELECT kind, value, unit, measured_at FROM fitness_measurements WHERE user_id=? AND kind='weight' AND measured_at>=? ORDER BY measured_at ASC, id ASC LIMIT 1000",
                (user_id, since),
            ).fetchall()
            legacy = conn.execute(
                "SELECT kind, COUNT(*) AS count FROM fitness_log WHERE user_id=? AND created_at>=? GROUP BY kind",
                (user_id, since),
            ).fetchall()
        return {
            "sessions": [dict(row) for row in sessions],
            "set_rows": [dict(row) for row in set_rows],
            "measurements": [dict(row) for row in measurements],
            "legacy": [dict(row) for row in legacy],
        }

    def latest_legacy_logs(self, user_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
        with db_connection() as conn:
            rows = conn.execute(
                "SELECT kind, value, detail, created_at FROM fitness_log WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_active_plan(self, user_id: str) -> dict[str, Any] | None:
        with db_connection() as conn:
            row = conn.execute(
                "SELECT id FROM fitness_plans WHERE user_id=? AND status='active' ORDER BY updated_at DESC, id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            return self._plan_payload(conn, int(row["id"]), user_id) if row else None

    def latest_in_progress_session(self, user_id: str) -> dict[str, Any] | None:
        with db_connection() as conn:
            row = conn.execute(
                "SELECT id FROM fitness_sessions WHERE user_id=? AND status='in_progress' ORDER BY started_at DESC, id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            return self._session_payload(conn, int(row["id"]), user_id) if row else None


__all__ = ["FitnessRepository", "SQLiteFitnessRepository"]
