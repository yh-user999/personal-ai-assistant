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
from app.fitness.repository import SQLiteFitnessRepository
from app.services.sanitize import sanitize

repository = SQLiteFitnessRepository()

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
    return repository.upsert_profile(
        uid,
        goal=goal,
        experience=experience,
        sessions_per_week=sessions_per_week,
        session_minutes=session_minutes,
        equipment=json.dumps(equipment, ensure_ascii=False),
        limitations=limitations,
        notes=notes,
        now=utc_iso(),
    )


def get_profile(user_id: str | None) -> dict[str, Any] | None:
    return repository.get_profile(_uid(user_id))


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

    return repository.create_plan(
        uid,
        name=name,
        goal=goal,
        source=source,
        notes=notes,
        days=normalized_days,
        now=utc_iso(),
    ) or {}


def list_plans(user_id: str | None, *, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    uid = _uid(user_id)
    if status and status not in PLAN_STATUSES:
        raise ValueError("非法计划状态")
    limit = max(1, min(int(limit), 50))
    return repository.list_plans(uid, status=status, limit=limit)


def get_plan(user_id: str | None, plan_id: int) -> dict[str, Any] | None:
    return repository.get_plan(_uid(user_id), int(plan_id))


def activate_plan(user_id: str | None, plan_id: int) -> dict[str, Any]:
    result = repository.activate_plan(_uid(user_id), int(plan_id), now=utc_iso())
    return result or {}


def archive_plan(user_id: str | None, plan_id: int) -> dict[str, Any]:
    result = repository.archive_plan(_uid(user_id), int(plan_id), now=utc_iso())
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
    return repository.start_session(
        uid,
        plan_id=plan_id,
        plan_day_id=plan_day_id,
        source=source,
        external_id=external_id,
        notes=notes,
        now=utc_iso(),
    ) or {}


def get_session(user_id: str | None, session_id: int) -> dict[str, Any] | None:
    return repository.get_session(_uid(user_id), int(session_id))


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
    return repository.list_sessions(uid, status=status, limit=limit)


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
    if set_order is not None:
        set_order = _int(set_order, name="组序号", minimum=1, maximum=MAX_SETS)
    return repository.log_set(
        uid,
        int(session_id),
        int(exercise_id),
        reps=reps,
        weight_kg=weight_kg,
        set_order=set_order,
        set_type=set_type,
        rpe=rpe,
        rir=rir,
        rest_seconds=rest_seconds,
        is_warmup=bool(is_warmup),
        note=note,
        now=utc_iso(),
    )


def complete_session(user_id: str | None, session_id: int) -> dict[str, Any]:
    return repository.complete_session(_uid(user_id), int(session_id), now=utc_iso()) or {}


def cancel_session(user_id: str | None, session_id: int) -> dict[str, Any]:
    return repository.cancel_session(_uid(user_id), int(session_id), now=utc_iso()) or {}


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
    return repository.record_measurement(
        uid,
        kind=kind,
        value=value,
        unit=unit,
        note=note,
        measured_at=measured_at,
        now=utc_iso(),
    ) or {}


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
    return repository.list_measurements(uid, kind=kind, limit=limit)


def get_summary(user_id: str | None, *, days: int = 30) -> dict[str, Any]:
    uid = _uid(user_id)
    days = max(1, min(int(days), 365))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = repository.summary_rows(uid, since=since)
    sessions = rows["sessions"]
    set_rows = rows["set_rows"]
    measurements = rows["measurements"]
    legacy = rows["legacy"]

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
    legacy_recent = repository.latest_legacy_logs(_uid(user_id), limit=5)
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
    return repository.get_active_plan(_uid(user_id))


def latest_in_progress_session(user_id: str | None) -> dict[str, Any] | None:
    return repository.latest_in_progress_session(_uid(user_id))


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
