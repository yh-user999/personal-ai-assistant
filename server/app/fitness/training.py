"""结构化健身训练领域服务。

本模块是本地健身数据的唯一写入边界：计划、训练会话、训练组和身体指标均按
user_id 隔离；旧的 fitness_log 自由文本台账继续由 fitness.py 维护。
"""
from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from app.common.timeutil import utc_iso
from app.models.database import db_connection
from app.services.sanitize import sanitize

PLAN_STATUSES = frozenset({"draft", "active", "archived"})
SESSION_STATUSES = frozenset({"in_progress", "completed", "cancelled"})
MEASUREMENT_KINDS = frozenset({"weight", "waist", "body_fat", "neck", "hip"})
MAX_PLAN_DAYS = 14
MAX_DAY_EXERCISES = 20
MAX_SETS = 20
MAX_REPS = 100
MAX_REST_SECONDS = 900
_MISSING = object()


def _uid(user_id: str | None) -> str:
    from app.core.memory import normalize_user_id

    return normalize_user_id(user_id)


def _text(value: Any, *, name: str, limit: int, required: bool = False) -> str:
    result = sanitize(str(value or "")).strip()[:limit]
    if required and not result:
        raise ValueError(f"{name}不能为空")
    return result


def _int(value: Any, *, name: str, minimum: int, maximum: int, default: int | None | object = _MISSING) -> int | None:
    if value is None and default is not _MISSING:
        return default
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}必须是整数") from exc
    if result < minimum or result > maximum:
        raise ValueError(f"{name}必须在{minimum}到{maximum}之间")
    return result


def _float(value: Any, *, name: str, minimum: float, maximum: float, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}必须是数字") from exc
    if result < minimum or result > maximum:
        raise ValueError(f"{name}必须在{minimum}到{maximum}之间")
    return round(result, 2)


def _json_list(value: Any) -> str:
    if value is None:
        value = []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = []
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray, str)):
        value = []
    return json.dumps(list(value), ensure_ascii=False, separators=(",", ":"))


def _decode_list(value: Any) -> list[Any]:
    try:
        result = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return result if isinstance(result, list) else []


def _parse_datetime(value: Any, *, name: str, default_now: bool = False) -> str:
    if value in (None, ""):
        if default_now:
            return utc_iso()
        raise ValueError(f"{name}不能为空")
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name}必须是 ISO 时间") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _exercise_exists(conn, exercise_id: int) -> bool:
    return bool(conn.execute("SELECT 1 FROM fitness_exercises WHERE id=?", (exercise_id,)).fetchone())


def _plan_payload(conn, plan_id: int, user_id: str) -> dict[str, Any] | None:
    plan = conn.execute(
        "SELECT id, user_id, name, goal, status, source, notes, version, created_at, updated_at "
        "FROM fitness_plans WHERE id=? AND user_id=?",
        (plan_id, user_id),
    ).fetchone()
    if not plan:
        return None
    days = conn.execute(
        "SELECT id, day_index, name, notes FROM fitness_plan_days WHERE plan_id=? ORDER BY day_index, id",
        (plan_id,),
    ).fetchall()
    result = dict(plan)
    result["days"] = []
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
            item["primary_muscles"] = _decode_list(item.get("primary_muscles"))
            day_payload["exercises"].append(item)
        result["days"].append(day_payload)
    return result


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


def upsert_profile(user_id: str | None, profile: Mapping[str, Any]) -> dict[str, Any]:
    uid = _uid(user_id)
    goal = _text(profile.get("goal"), name="目标", limit=200)
    experience = _text(profile.get("experience"), name="训练经验", limit=120)
    sessions_per_week = _int(profile.get("sessions_per_week"), name="每周训练次数", minimum=1, maximum=7, default=None)
    session_minutes = _int(profile.get("session_minutes"), name="单次训练时长", minimum=10, maximum=240, default=None)
    equipment = profile.get("equipment", [])
    if isinstance(equipment, str):
        equipment = [item.strip() for item in equipment.replace("，", ",").split(",") if item.strip()]
    if not isinstance(equipment, Sequence) or isinstance(equipment, (bytes, bytearray, str)):
        equipment = []
    equipment = [_text(item, name="器械", limit=80) for item in list(equipment)[:32] if str(item).strip()]
    limitations = _text(profile.get("limitations"), name="限制条件", limit=500)
    notes = _text(profile.get("notes"), name="备注", limit=1000)
    now = utc_iso()
    with db_connection() as conn:
        conn.execute(
            "INSERT INTO fitness_profile(user_id, goal, experience, sessions_per_week, session_minutes, equipment, "
            "limitations, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET goal=excluded.goal, experience=excluded.experience, "
            "sessions_per_week=excluded.sessions_per_week, session_minutes=excluded.session_minutes, "
            "equipment=excluded.equipment, limitations=excluded.limitations, notes=excluded.notes, updated_at=excluded.updated_at",
            (uid, goal, experience, sessions_per_week, session_minutes, json.dumps(equipment, ensure_ascii=False), limitations, notes, now, now),
        )
        row = conn.execute("SELECT * FROM fitness_profile WHERE user_id=?", (uid,)).fetchone()
    result = dict(row)
    result["equipment"] = _decode_list(result.get("equipment"))
    return result


def get_profile(user_id: str | None) -> dict[str, Any] | None:
    uid = _uid(user_id)
    with db_connection() as conn:
        row = conn.execute("SELECT * FROM fitness_profile WHERE user_id=?", (uid,)).fetchone()
    if not row:
        return None
    result = dict(row)
    result["equipment"] = _decode_list(result.get("equipment"))
    return result


def create_plan(user_id: str | None, plan: Mapping[str, Any]) -> dict[str, Any]:
    uid = _uid(user_id)
    name = _text(plan.get("name"), name="计划名称", limit=160, required=True)
    goal = _text(plan.get("goal"), name="计划目标", limit=200)
    notes = _text(plan.get("notes"), name="计划备注", limit=1000)
    source = _text(plan.get("source") or "user", name="计划来源", limit=80)
    days = plan.get("days")
    if not isinstance(days, Sequence) or isinstance(days, (bytes, bytearray, str)) or not days:
        raise ValueError("计划至少需要一个训练日")
    if len(days) > MAX_PLAN_DAYS:
        raise ValueError(f"计划最多包含{MAX_PLAN_DAYS}个训练日")

    normalized_days: list[dict[str, Any]] = []
    seen_day_indexes: set[int] = set()
    with db_connection() as conn:
        for position, raw_day in enumerate(days, 1):
            if not isinstance(raw_day, Mapping):
                raise ValueError("训练日必须是对象")
            day_index = _int(raw_day.get("day_index", position), name="训练日序号", minimum=1, maximum=MAX_PLAN_DAYS)
            if day_index in seen_day_indexes:
                raise ValueError("训练日序号不能重复")
            seen_day_indexes.add(day_index)
            day_name = _text(raw_day.get("name") or f"训练日 {day_index}", name="训练日名称", limit=160, required=True)
            day_notes = _text(raw_day.get("notes"), name="训练日备注", limit=500)
            raw_exercises = raw_day.get("exercises", [])
            if not isinstance(raw_exercises, Sequence) or isinstance(raw_exercises, (bytes, bytearray, str)):
                raise ValueError("训练日动作必须是数组")
            if len(raw_exercises) > MAX_DAY_EXERCISES:
                raise ValueError(f"单个训练日最多{MAX_DAY_EXERCISES}个动作")
            normalized_exercises: list[dict[str, Any]] = []
            seen_orders: set[int] = set()
            for order, raw_exercise in enumerate(raw_exercises, 1):
                if not isinstance(raw_exercise, Mapping):
                    raise ValueError("计划动作必须是对象")
                exercise_id = _int(raw_exercise.get("exercise_id"), name="动作 ID", minimum=1, maximum=2_147_483_647)
                if not _exercise_exists(conn, exercise_id):
                    raise ValueError(f"动作不存在：{exercise_id}")
                sort_order = _int(raw_exercise.get("sort_order", order), name="动作顺序", minimum=1, maximum=MAX_DAY_EXERCISES)
                if sort_order in seen_orders:
                    raise ValueError("动作顺序不能重复")
                seen_orders.add(sort_order)
                sets = _int(raw_exercise.get("sets"), name="组数", minimum=1, maximum=MAX_SETS)
                rep_min = _int(raw_exercise.get("rep_min", raw_exercise.get("reps", 8)), name="最低次数", minimum=1, maximum=MAX_REPS)
                rep_max = _int(raw_exercise.get("rep_max", raw_exercise.get("reps", rep_min)), name="最高次数", minimum=rep_min, maximum=MAX_REPS)
                rir_target = _float(raw_exercise.get("rir_target"), name="目标 RIR", minimum=0, maximum=5)
                rest_seconds = _int(raw_exercise.get("rest_seconds"), name="组间休息", minimum=0, maximum=MAX_REST_SECONDS, default=None)
                exercise_notes = _text(raw_exercise.get("notes"), name="动作备注", limit=500)
                normalized_exercises.append({
                    "exercise_id": exercise_id,
                    "sort_order": sort_order,
                    "sets": sets,
                    "rep_min": rep_min,
                    "rep_max": rep_max,
                    "rir_target": rir_target,
                    "rest_seconds": rest_seconds,
                    "notes": exercise_notes,
                })
            normalized_days.append({"day_index": day_index, "name": day_name, "notes": day_notes, "exercises": normalized_exercises})

        now = utc_iso()
        cur = conn.execute(
            "INSERT INTO fitness_plans(user_id, name, goal, status, source, notes, version, created_at, updated_at) "
            "VALUES (?, ?, ?, 'draft', ?, ?, 1, ?, ?)",
            (uid, name, goal, source, notes, now, now),
        )
        plan_id = int(cur.lastrowid)
        for day in normalized_days:
            day_cur = conn.execute(
                "INSERT INTO fitness_plan_days(plan_id, day_index, name, notes) VALUES (?, ?, ?, ?)",
                (plan_id, day["day_index"], day["name"], day["notes"]),
            )
            day_id = int(day_cur.lastrowid)
            for item in day["exercises"]:
                conn.execute(
                    "INSERT INTO fitness_plan_exercises(plan_day_id, exercise_id, sort_order, sets, rep_min, rep_max, "
                    "rir_target, rest_seconds, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        day_id,
                        item["exercise_id"],
                        item["sort_order"],
                        item["sets"],
                        item["rep_min"],
                        item["rep_max"],
                        item["rir_target"],
                        item["rest_seconds"],
                        item["notes"],
                    ),
                )
        result = _plan_payload(conn, plan_id, uid)
    return result or {}


def list_plans(user_id: str | None, *, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    uid = _uid(user_id)
    if status and status not in PLAN_STATUSES:
        raise ValueError("非法计划状态")
    limit = max(1, min(int(limit), 50))
    clauses = ["user_id=?"]
    args: list[Any] = [uid]
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


def get_plan(user_id: str | None, plan_id: int) -> dict[str, Any] | None:
    uid = _uid(user_id)
    with db_connection() as conn:
        return _plan_payload(conn, int(plan_id), uid)


def activate_plan(user_id: str | None, plan_id: int) -> dict[str, Any]:
    uid = _uid(user_id)
    with db_connection() as conn:
        target = conn.execute(
            "SELECT id FROM fitness_plans WHERE id=? AND user_id=?", (int(plan_id), uid)
        ).fetchone()
        if not target:
            raise KeyError("计划不存在")
        now = utc_iso()
        conn.execute(
            "UPDATE fitness_plans SET status='archived', version=version+1, updated_at=? "
            "WHERE user_id=? AND status='active' AND id<>?",
            (now, uid, int(plan_id)),
        )
        conn.execute(
            "UPDATE fitness_plans SET status='active', version=version+1, updated_at=? WHERE id=? AND user_id=?",
            (now, int(plan_id), uid),
        )
        result = _plan_payload(conn, int(plan_id), uid)
    return result or {}


def archive_plan(user_id: str | None, plan_id: int) -> dict[str, Any]:
    uid = _uid(user_id)
    with db_connection() as conn:
        cur = conn.execute(
            "UPDATE fitness_plans SET status='archived', version=version+1, updated_at=? "
            "WHERE id=? AND user_id=? AND status<>'archived'",
            (utc_iso(), int(plan_id), uid),
        )
        if not cur.rowcount:
            raise KeyError("计划不存在")
        result = _plan_payload(conn, int(plan_id), uid)
    return result or {}


def start_session(
    user_id: str | None,
    *,
    plan_id: int | None = None,
    plan_day_id: int | None = None,
    source: str = "local",
    external_id: str | None = None,
    notes: str = "",
) -> dict[str, Any]:
    uid = _uid(user_id)
    source = _text(source or "local", name="来源", limit=80, required=True)
    external_id = _text(external_id, name="外部 ID", limit=200) or None
    notes = _text(notes, name="备注", limit=1000)
    with db_connection() as conn:
        if external_id:
            existing = conn.execute(
                "SELECT id FROM fitness_sessions WHERE user_id=? AND source=? AND external_id=?",
                (uid, source, external_id),
            ).fetchone()
            if existing:
                return _session_payload(conn, int(existing["id"]), uid) or {}
        if plan_id is not None:
            plan = conn.execute(
                "SELECT id FROM fitness_plans WHERE id=? AND user_id=?", (int(plan_id), uid)
            ).fetchone()
            if not plan:
                raise KeyError("计划不存在")
        if plan_day_id is not None:
            day = conn.execute(
                "SELECT d.id, d.plan_id FROM fitness_plan_days d JOIN fitness_plans p ON p.id=d.plan_id "
                "WHERE d.id=? AND p.user_id=?",
                (int(plan_day_id), uid),
            ).fetchone()
            if not day:
                raise KeyError("训练日不存在")
            if plan_id is not None and int(day["plan_id"]) != int(plan_id):
                raise ValueError("训练日不属于指定计划")
            plan_id = int(day["plan_id"])
        now = utc_iso()
        cur = conn.execute(
            "INSERT INTO fitness_sessions(user_id, plan_id, plan_day_id, status, source, external_id, notes, started_at) "
            "VALUES (?, ?, ?, 'in_progress', ?, ?, ?, ?)",
            (uid, plan_id, plan_day_id, source, external_id, notes, now),
        )
        return _session_payload(conn, int(cur.lastrowid), uid) or {}


def get_session(user_id: str | None, session_id: int) -> dict[str, Any] | None:
    uid = _uid(user_id)
    with db_connection() as conn:
        return _session_payload(conn, int(session_id), uid)


def list_sessions(
    user_id: str | None,
    *,
    status: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    uid = _uid(user_id)
    if status and status not in SESSION_STATUSES:
        raise ValueError("非法训练会话状态")
    limit = max(1, min(int(limit), 50))
    clauses = ["user_id=?"]
    args: list[Any] = [uid]
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
        return [_session_payload(conn, int(row["id"]), uid) or {} for row in rows]


def log_set(
    user_id: str | None,
    session_id: int,
    exercise_id: int,
    *,
    reps: int,
    weight_kg: float = 0,
    set_order: int | None = None,
    set_type: str = "working",
    rpe: float | None = None,
    rir: float | None = None,
    rest_seconds: int | None = None,
    is_warmup: bool = False,
    note: str = "",
) -> dict[str, Any]:
    uid = _uid(user_id)
    reps = _int(reps, name="次数", minimum=1, maximum=MAX_REPS)
    weight_kg = _float(weight_kg, name="重量", minimum=0, maximum=1000, default=0) or 0
    rpe = _float(rpe, name="RPE", minimum=0, maximum=10)
    rir = _float(rir, name="RIR", minimum=0, maximum=10)
    rest_seconds = _int(rest_seconds, name="休息时间", minimum=0, maximum=MAX_REST_SECONDS, default=None)
    set_type = _text(set_type or "working", name="组类型", limit=40, required=True).casefold()
    note = _text(note, name="备注", limit=500)
    with db_connection() as conn:
        session = conn.execute(
            "SELECT id, status FROM fitness_sessions WHERE id=? AND user_id=?", (int(session_id), uid)
        ).fetchone()
        if not session:
            raise KeyError("训练会话不存在")
        if session["status"] != "in_progress":
            raise ValueError("只有进行中的训练会话可以记录训练组")
        if not _exercise_exists(conn, int(exercise_id)):
            raise KeyError("动作不存在")
        if set_order is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(set_order), 0) + 1 AS next_order FROM fitness_sets "
                "WHERE session_id=? AND exercise_id=?",
                (int(session_id), int(exercise_id)),
            ).fetchone()
            set_order = int(row["next_order"])
        else:
            set_order = _int(set_order, name="组序号", minimum=1, maximum=MAX_SETS)
        cur = conn.execute(
            "INSERT INTO fitness_sets(session_id, exercise_id, set_order, set_type, reps, weight_kg, rpe, rir, "
            "rest_seconds, is_warmup, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (int(session_id), int(exercise_id), set_order, set_type, reps, weight_kg, rpe, rir, rest_seconds, int(is_warmup), note, utc_iso()),
        )
        row = conn.execute(
            "SELECT fs.id, fs.session_id, fs.exercise_id, fs.set_order, fs.set_type, fs.reps, fs.weight_kg, "
            "fs.rpe, fs.rir, fs.rest_seconds, fs.is_warmup, fs.note, fs.created_at, e.name AS exercise_name "
            "FROM fitness_sets fs JOIN fitness_exercises e ON e.id=fs.exercise_id WHERE fs.id=?",
            (int(cur.lastrowid),),
        ).fetchone()
    return dict(row)


def complete_session(user_id: str | None, session_id: int) -> dict[str, Any]:
    uid = _uid(user_id)
    with db_connection() as conn:
        session = conn.execute(
            "SELECT status FROM fitness_sessions WHERE id=? AND user_id=?", (int(session_id), uid)
        ).fetchone()
        if not session:
            raise KeyError("训练会话不存在")
        if session["status"] == "cancelled":
            raise ValueError("已取消的训练会话不能完成")
        if session["status"] == "in_progress":
            conn.execute(
                "UPDATE fitness_sessions SET status='completed', completed_at=? WHERE id=? AND user_id=?",
                (utc_iso(), int(session_id), uid),
            )
        return _session_payload(conn, int(session_id), uid) or {}


def cancel_session(user_id: str | None, session_id: int) -> dict[str, Any]:
    uid = _uid(user_id)
    with db_connection() as conn:
        cur = conn.execute(
            "UPDATE fitness_sessions SET status='cancelled', completed_at=COALESCE(completed_at, ?) "
            "WHERE id=? AND user_id=? AND status='in_progress'",
            (utc_iso(), int(session_id), uid),
        )
        if not cur.rowcount:
            raise KeyError("进行中的训练会话不存在")
        return _session_payload(conn, int(session_id), uid) or {}


def record_measurement(
    user_id: str | None,
    *,
    kind: str,
    value: float,
    unit: str | None = None,
    measured_at: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    uid = _uid(user_id)
    kind = _text(kind, name="指标类型", limit=40, required=True).casefold()
    if kind not in MEASUREMENT_KINDS:
        raise ValueError("指标类型只能是 weight、waist、body_fat、neck 或 hip")
    ranges = {
        "weight": (20, 300, "kg"),
        "waist": (30, 300, "cm"),
        "body_fat": (1, 80, "%"),
        "neck": (20, 100, "cm"),
        "hip": (30, 300, "cm"),
    }
    minimum, maximum, default_unit = ranges[kind]
    value = _float(value, name="指标值", minimum=minimum, maximum=maximum) or 0
    unit = _text(unit or default_unit, name="单位", limit=20, required=True)
    note = _text(note, name="备注", limit=500)
    measured_at = _parse_datetime(measured_at, name="测量时间", default_now=True)
    now = utc_iso()
    with db_connection() as conn:
        cur = conn.execute(
            "INSERT INTO fitness_measurements(user_id, kind, value, unit, note, measured_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uid, kind, value, unit, note, measured_at, now),
        )
        # 旧版命令读取 fitness_log；体重 API 同步一份，保持两个入口的趋势一致。
        if kind == "weight" and unit.casefold() in {"kg", "公斤", "千克"}:
            conn.execute(
                "INSERT INTO fitness_log(user_id, kind, value, detail, created_at) VALUES (?, 'weight', ?, '', ?)",
                (uid, value, measured_at),
            )
        row = conn.execute(
            "SELECT id, user_id, kind, value, unit, note, measured_at, created_at "
            "FROM fitness_measurements WHERE id=?",
            (int(cur.lastrowid),),
        ).fetchone()
    return dict(row)


def list_measurements(
    user_id: str | None,
    *,
    kind: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    uid = _uid(user_id)
    if kind and kind not in MEASUREMENT_KINDS:
        raise ValueError("非法指标类型")
    limit = max(1, min(int(limit), 100))
    args: list[Any] = [uid]
    clause = "user_id=?"
    if kind:
        clause += " AND kind=?"
        args.append(kind)
    args.append(limit)
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT id, user_id, kind, value, unit, note, measured_at, created_at "
            f"FROM fitness_measurements WHERE {clause} ORDER BY measured_at DESC, id DESC LIMIT ?",
            args,
        ).fetchall()
    return [dict(row) for row in rows]


def get_summary(user_id: str | None, *, days: int = 30) -> dict[str, Any]:
    uid = _uid(user_id)
    days = max(1, min(int(days), 365))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db_connection() as conn:
        sessions = conn.execute(
            "SELECT id, status, started_at, completed_at FROM fitness_sessions "
            "WHERE user_id=? AND started_at>=? AND status<>'cancelled' "
            "ORDER BY started_at DESC, id DESC",
            (uid, since),
        ).fetchall()
        set_rows = conn.execute(
            "SELECT fs.exercise_id, fs.reps, fs.weight_kg, fs.rpe, fs.rir, fs.is_warmup, fs.created_at, "
            "e.name, e.primary_muscles FROM fitness_sets fs "
            "JOIN fitness_sessions s ON s.id=fs.session_id "
            "JOIN fitness_exercises e ON e.id=fs.exercise_id "
            "WHERE s.user_id=? AND s.started_at>=? AND s.status<>'cancelled'",
            (uid, since),
        ).fetchall()
        measurements = conn.execute(
            "SELECT kind, value, unit, measured_at FROM fitness_measurements "
            "WHERE user_id=? AND kind='weight' AND measured_at>=? "
            "ORDER BY measured_at ASC, id ASC LIMIT 1000",
            (uid, since),
        ).fetchall()
        legacy = conn.execute(
            "SELECT kind, COUNT(*) AS count FROM fitness_log WHERE user_id=? AND created_at>=? GROUP BY kind",
            (uid, since),
        ).fetchall()

    completed = sum(1 for row in sessions if row["status"] == "completed")
    in_progress = sum(1 for row in sessions if row["status"] == "in_progress")
    volume = round(sum((row["reps"] or 0) * (row["weight_kg"] or 0) for row in set_rows if not row["is_warmup"]), 2)
    muscle_sets: defaultdict[str, int] = defaultdict(int)
    exercise_stats: dict[int, dict[str, Any]] = {}
    for row in set_rows:
        if row["is_warmup"]:
            continue
        muscles = _decode_list(row["primary_muscles"])
        for muscle in muscles:
            muscle_sets[str(muscle)] += 1
        stats = exercise_stats.setdefault(int(row["exercise_id"]), {"name": row["name"], "max_weight_kg": 0.0, "estimated_1rm": 0.0, "best_reps": 0})
        weight = float(row["weight_kg"] or 0)
        reps = int(row["reps"] or 0)
        stats["max_weight_kg"] = max(stats["max_weight_kg"], weight)
        stats["best_reps"] = max(stats["best_reps"], reps)
        stats["estimated_1rm"] = max(stats["estimated_1rm"], weight * (1 + reps / 30) if weight and reps else 0)
    prs = sorted(
        (
            {**value, "max_weight_kg": round(value["max_weight_kg"], 2), "estimated_1rm": round(value["estimated_1rm"], 2)}
            for value in exercise_stats.values()
        ),
        key=lambda item: (item["max_weight_kg"], item["estimated_1rm"]),
        reverse=True,
    )[:10]

    weight_trend: dict[str, Any] | None = None
    if measurements:
        first = measurements[0]
        latest = measurements[-1]
        weight_trend = {
            "first": first["value"],
            "latest": latest["value"],
            "delta": round(float(latest["value"]) - float(first["value"]), 2),
            "unit": latest["unit"],
            "first_at": first["measured_at"],
            "latest_at": latest["measured_at"],
        }

    last_training_at = sessions[0]["started_at"] if sessions else None
    days_since_training = None
    if last_training_at:
        try:
            last = datetime.fromisoformat(last_training_at)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            days_since_training = max(0, (datetime.now(timezone.utc) - last).days)
        except ValueError:
            days_since_training = None
    legacy_counts = {row["kind"]: row["count"] for row in legacy}
    return {
        "days": days,
        "sessions": {
            "total": len(sessions),
            "completed": completed,
            "in_progress": in_progress,
            "completion_rate": round(completed / len(sessions), 3) if sessions else 0,
        },
        "sets": len(set_rows),
        "volume_kg": volume,
        "muscle_sets": dict(sorted(muscle_sets.items(), key=lambda item: (-item[1], item[0]))),
        "recent_prs": prs,
        "weight_trend": weight_trend,
        "last_training_at": last_training_at,
        "days_since_training": days_since_training,
        "legacy_counts": legacy_counts,
    }


def summary_text(user_id: str | None, *, days: int = 30) -> str:
    summary = get_summary(user_id, days=days)
    sessions = summary["sessions"]
    if not sessions["total"] and not summary["legacy_counts"] and not summary["weight_trend"]:
        return "🏋️ 还没有结构化健身记录。可以先导入动作库，再创建训练计划。"
    lines = [f"🏋️ 近 {summary['days']} 天健身概览：训练 {sessions['total']} 次，完成 {sessions['completed']} 次"]
    if summary["sets"]:
        lines.append(f"训练组 {summary['sets']} 组，总训练量 {summary['volume_kg']:.1f} kg")
    if summary["muscle_sets"]:
        top = list(summary["muscle_sets"].items())[:4]
        lines.append("主要肌群容量：" + "、".join(f"{name} {count} 组" for name, count in top))
    if summary["weight_trend"]:
        trend = summary["weight_trend"]
        lines.append(f"体重：{trend['latest']} {trend['unit']}（区间变化 {trend['delta']:+.1f}）")
    if summary["days_since_training"] is not None and summary["days_since_training"] >= 7:
        lines.append("⚠️ 最近 7 天没有新的训练会话，恢复训练建议从低容量开始。")
    if summary["recent_prs"]:
        lines.append("近期高重量动作：" + "、".join(f"{item['name']} {item['max_weight_kg']:.1f} kg" for item in summary["recent_prs"][:3]))
    with db_connection() as conn:
        legacy_recent = conn.execute(
            "SELECT kind, value, detail, created_at FROM fitness_log WHERE user_id=? "
            "ORDER BY id DESC LIMIT 5",
            (_uid(user_id),),
        ).fetchall()
    if legacy_recent:
        lines.append("最近记录：")
        for row in legacy_recent:
            date_text = str(row["created_at"] or "")[:10]
            date_text = date_text[5:] if len(date_text) >= 10 else date_text
            if row["kind"] == "weight":
                lines.append(f"{date_text} 体重 {row['value']} kg")
            else:
                lines.append(f"{date_text} 训练：{row['detail']}")
    return "\n".join(lines)


def get_active_plan(user_id: str | None) -> dict[str, Any] | None:
    uid = _uid(user_id)
    with db_connection() as conn:
        row = conn.execute(
            "SELECT id FROM fitness_plans WHERE user_id=? AND status='active' "
            "ORDER BY updated_at DESC, id DESC LIMIT 1",
            (uid,),
        ).fetchone()
        return _plan_payload(conn, int(row["id"]), uid) if row else None


def latest_in_progress_session(user_id: str | None) -> dict[str, Any] | None:
    uid = _uid(user_id)
    with db_connection() as conn:
        row = conn.execute(
            "SELECT id FROM fitness_sessions WHERE user_id=? AND status='in_progress' "
            "ORDER BY started_at DESC, id DESC LIMIT 1",
            (uid,),
        ).fetchone()
        return _session_payload(conn, int(row["id"]), uid) if row else None


def parse_chat_set(text: str) -> dict[str, Any] | None:
    """解析简单的「记录组：卧推 60kg x 8」表达，不解析不确定的自然语言。"""
    import re

    value = str(text or "").strip()
    if value.startswith(("记录组", "训练组")):
        value = re.sub(r"^(?:记录组|训练组)[:：]?\s*", "", value).strip()
    if not value:
        return None
    match = re.match(
        r"^(.+?)\s+(?:(?:重量|负重)\s*)?(\d+(?:\.\d+)?)\s*(?:kg|公斤|千克)?\s*[x×*]\s*(\d+)(?:\s*(?:次|reps?))?"
        r"(?:\s+(?:RIR|rir)\s*(\d+(?:\.\d+)?))?$",
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        match = re.match(
            r"^(.+?)\s*[，,]\s*(?:重量|负重)[:：]?\s*(\d+(?:\.\d+)?)\s*(?:kg|公斤|千克)?"
            r"\s*[，,]\s*(?:次数|reps?)[:：]?\s*(\d+)(?:\s*[，,]\s*(?:RIR|rir)[:：]?\s*(\d+(?:\.\d+)?))?$",
            value,
            flags=re.IGNORECASE,
        )
    if not match:
        return None
    name, weight, reps, rir = match.groups()
    return {
        "exercise_name": name.strip()[:160],
        "weight_kg": float(weight),
        "reps": int(reps),
        "rir": float(rir) if rir is not None else None,
    }
