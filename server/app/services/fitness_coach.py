"""AI 健身计划草案：只生成并校验，不直接写入数据库。"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.core import llm
from app.services import fitness_catalog, fitness_training

_MISSING = object()


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


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
    if value in (None, ""):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}必须是数字") from exc
    if result < minimum or result > maximum:
        raise ValueError(f"{name}必须在{minimum}到{maximum}之间")
    return round(result, 2)


def _candidate_exercises(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    fitness_catalog.ensure_builtin_exercises()
    equipment = request.get("equipment", [])
    if isinstance(equipment, str):
        equipment = [item.strip() for item in equipment.replace("，", ",").split(",") if item.strip()]
    if not isinstance(equipment, Sequence) or isinstance(equipment, (bytes, bytearray, str)):
        equipment = []
    equipment_terms = [str(item).strip().casefold() for item in list(equipment)[:16] if str(item).strip()]
    all_items = fitness_catalog.list_exercises(limit=100)
    if not equipment_terms:
        return all_items[:60]
    filtered = [
        item for item in all_items
        if not item.get("equipment")
        or item["equipment"].casefold() in equipment_terms
        or any(term in item["equipment"].casefold() for term in equipment_terms)
    ]
    return filtered[:60] or all_items[:60]


def _prompt(request: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]) -> str:
    candidate_lines = []
    for item in candidates:
        candidate_lines.append(
            f"id={item['id']}; name={item['name']}; primary={','.join(item.get('primary_muscles', []))}; "
            f"equipment={item.get('equipment', '')}"
        )
    return (
        "你是保守、可执行的私人健身教练。根据用户条件生成一个结构化训练计划草案。\n"
        "只允许从候选动作中选择，不能创造动作 ID；不得给出医疗诊断。\n"
        "要求：每个训练日 2~8 个动作；每个动作 1~6 组、每组 1~30 次；"
        "复合动作通常 6~12 次，孤立动作通常 10~15 次；目标 RIR 0~4；"
        "休息 0~600 秒；控制总训练时长和每周训练频率。\n"
        "只输出 JSON，不要 Markdown。格式："
        '{"name":"...","goal":"...","notes":"...","days":['
        '{"day_index":1,"name":"...","notes":"...","exercises":['
        '{"exercise_id":1,"sort_order":1,"sets":3,"rep_min":8,"rep_max":12,"rir_target":2,"rest_seconds":120,"notes":""}'
        "]}]}\n"
        f"用户条件：目标={_text(request.get('goal'), 200)}；训练经验={_text(request.get('experience'), 120)}；"
        f"每周训练次数={request.get('sessions_per_week')}; 单次分钟={request.get('session_minutes')};"
        f"器械={_text(','.join(map(str, request.get('equipment', []))) if isinstance(request.get('equipment', []), Sequence) and not isinstance(request.get('equipment', []), str) else request.get('equipment'), 300)};"
        f"限制条件={_text(request.get('limitations'), 500)}。\n"
        "候选动作：\n" + "\n".join(candidate_lines)
    )


def _decode_json(content: str) -> Any:
    text = str(content or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("AI 返回的训练计划不是合法 JSON") from exc


def validate_plan_draft(payload: Any, *, candidate_ids: set[int]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("AI 计划必须是对象")
    name = _text(payload.get("name"), 160)
    goal = _text(payload.get("goal"), 200)
    notes = _text(payload.get("notes"), 1000)
    if not name:
        raise ValueError("AI 计划缺少名称")
    days = payload.get("days")
    if not isinstance(days, Sequence) or isinstance(days, (str, bytes, bytearray)) or not days:
        raise ValueError("AI 计划至少需要一个训练日")
    if len(days) > fitness_training.MAX_PLAN_DAYS:
        raise ValueError("AI 计划训练日过多")
    normalized_days: list[dict[str, Any]] = []
    seen_days: set[int] = set()
    for position, raw_day in enumerate(days, 1):
        if not isinstance(raw_day, Mapping):
            raise ValueError("AI 训练日格式错误")
        day_index = _int(raw_day.get("day_index", position), name="训练日序号", minimum=1, maximum=fitness_training.MAX_PLAN_DAYS)
        if day_index in seen_days:
            raise ValueError("AI 训练日序号重复")
        seen_days.add(day_index)
        raw_exercises = raw_day.get("exercises")
        if not isinstance(raw_exercises, Sequence) or isinstance(raw_exercises, (str, bytes, bytearray)) or not raw_exercises:
            raise ValueError("AI 训练日缺少动作")
        if len(raw_exercises) > 8:
            raise ValueError("AI 单日动作过多")
        normalized_exercises: list[dict[str, Any]] = []
        seen_orders: set[int] = set()
        for order, raw_exercise in enumerate(raw_exercises, 1):
            if not isinstance(raw_exercise, Mapping):
                raise ValueError("AI 动作格式错误")
            exercise_id = _int(raw_exercise.get("exercise_id"), name="动作 ID", minimum=1, maximum=2_147_483_647)
            if exercise_id not in candidate_ids:
                raise ValueError(f"AI 使用了候选列表之外的动作：{exercise_id}")
            sort_order = _int(raw_exercise.get("sort_order", order), name="动作顺序", minimum=1, maximum=8)
            if sort_order in seen_orders:
                raise ValueError("AI 动作顺序重复")
            seen_orders.add(sort_order)
            sets = _int(raw_exercise.get("sets"), name="组数", minimum=1, maximum=6)
            rep_min = _int(raw_exercise.get("rep_min", raw_exercise.get("reps", 8)), name="最低次数", minimum=1, maximum=30)
            rep_max = _int(raw_exercise.get("rep_max", raw_exercise.get("reps", rep_min)), name="最高次数", minimum=rep_min, maximum=30)
            rir_target = _float(raw_exercise.get("rir_target"), name="目标 RIR", minimum=0, maximum=4)
            rest_seconds = _int(raw_exercise.get("rest_seconds"), name="组间休息", minimum=0, maximum=600, default=None)
            normalized_exercises.append({
                "exercise_id": exercise_id,
                "sort_order": sort_order,
                "sets": sets,
                "rep_min": rep_min,
                "rep_max": rep_max,
                "rir_target": rir_target,
                "rest_seconds": rest_seconds,
                "notes": _text(raw_exercise.get("notes"), 500),
            })
        normalized_days.append({
            "day_index": day_index,
            "name": _text(raw_day.get("name") or f"训练日 {day_index}", 160),
            "notes": _text(raw_day.get("notes"), 500),
            "exercises": normalized_exercises,
        })
    return {"name": name, "goal": goal, "notes": notes, "source": "ai", "days": normalized_days}


async def generate_plan_draft(
    user_id: str | None,
    request: Mapping[str, Any],
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    from app.core.memory import normalize_user_id
    from app.services.llm_usage import logical_request_id

    uid = normalize_user_id(user_id)
    profile = fitness_training.get_profile(uid) or {}
    merged = dict(profile)
    merged.update({key: value for key, value in request.items() if value not in (None, "", [], {})})
    merged["goal"] = _text(merged.get("goal") or "减脂保肌", 200)
    merged["sessions_per_week"] = _int(merged.get("sessions_per_week", 3), name="每周训练次数", minimum=1, maximum=7)
    merged["session_minutes"] = _int(merged.get("session_minutes", 45), name="单次训练时长", minimum=10, maximum=180)
    candidates = _candidate_exercises(merged)
    if not candidates:
        raise ValueError("本地动作库为空，无法生成计划")
    candidate_ids = {int(item["id"]) for item in candidates}
    content = await llm.chat(
        [
            {"role": "system", "content": "你只输出符合要求的 JSON 训练计划，不输出解释。"},
            {"role": "user", "content": _prompt(merged, candidates)},
        ],
        temperature=0.2,
        max_tokens=2500,
        response_format={"type": "json_object"},
        request_id=request_id or logical_request_id("fitness_plan", uid, "draft"),
        user_id=uid,
    )
    draft = validate_plan_draft(_decode_json(content), candidate_ids=candidate_ids)
    return {"draft": draft, "candidate_count": len(candidates)}
