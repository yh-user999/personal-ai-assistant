"""结构化健身 API：动作目录、训练计划、训练会话和身体指标。"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth import require_roles
from app.core.memory import owner_user_id
from app.services import fitness_catalog, fitness_coach, fitness_nutrition, fitness_training

router = APIRouter()


class ProfileRequest(BaseModel):
    goal: str = Field("", max_length=200)
    experience: str = Field("", max_length=120)
    sessions_per_week: int | None = Field(None, ge=1, le=7)
    session_minutes: int | None = Field(None, ge=10, le=240)
    equipment: list[str] = Field(default_factory=list, max_length=32)
    limitations: str = Field("", max_length=500)
    notes: str = Field("", max_length=1000)


class ImportRequest(BaseModel):
    records: list[dict[str, Any]] = Field(default_factory=list, max_length=5000)
    source: str = Field("external", min_length=1, max_length=80)
    license_name: str = Field("", max_length=160)
    attribution: str = Field("", max_length=160)
    external_id: str | None = Field(None, max_length=200)


class PreviewRequest(BaseModel):
    records: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    source: str = Field("external", min_length=1, max_length=80)
    license_name: str = Field("", max_length=160)
    attribution: str = Field("", max_length=160)


class FoodImportRequest(BaseModel):
    records: list[dict[str, Any]] = Field(default_factory=list, max_length=5000)
    source: str = Field("external", min_length=1, max_length=80)
    license_name: str = Field("", max_length=160)
    attribution: str = Field("", max_length=300)
    skip_invalid: bool = False


class NutritionLogRequest(BaseModel):
    food_id: int = Field(..., ge=1)
    grams: float = Field(..., gt=0, le=5000)
    meal: str = Field("", max_length=40)
    source: str = Field("local", min_length=1, max_length=80)
    external_id: str | None = Field(None, max_length=200)
    eaten_at: str | None = Field(None, max_length=80)
    note: str = Field("", max_length=500)


class PlanExerciseRequest(BaseModel):
    exercise_id: int = Field(..., ge=1)
    sort_order: int | None = Field(None, ge=1, le=20)
    sets: int = Field(..., ge=1, le=20)
    rep_min: int = Field(1, ge=1, le=100)
    rep_max: int | None = Field(None, ge=1, le=100)
    reps: int | None = Field(None, ge=1, le=100)
    rir_target: float | None = Field(None, ge=0, le=5)
    rest_seconds: int | None = Field(None, ge=0, le=900)
    notes: str = Field("", max_length=500)


class PlanDayRequest(BaseModel):
    day_index: int | None = Field(None, ge=1, le=14)
    name: str = Field("", max_length=160)
    notes: str = Field("", max_length=500)
    exercises: list[PlanExerciseRequest] = Field(default_factory=list, max_length=20)


class PlanRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    goal: str = Field("", max_length=200)
    notes: str = Field("", max_length=1000)
    source: str = Field("user", max_length=80)
    days: list[PlanDayRequest] = Field(..., min_length=1, max_length=14)


class GeneratePlanRequest(BaseModel):
    goal: str = Field("减脂保肌", max_length=200)
    experience: str = Field("", max_length=120)
    sessions_per_week: int = Field(3, ge=1, le=7)
    session_minutes: int = Field(45, ge=10, le=180)
    equipment: list[str] = Field(default_factory=list, max_length=16)
    limitations: str = Field("", max_length=500)


class SessionRequest(BaseModel):
    plan_id: int | None = Field(None, ge=1)
    plan_day_id: int | None = Field(None, ge=1)
    source: str = Field("local", max_length=80)
    external_id: str | None = Field(None, max_length=200)
    notes: str = Field("", max_length=1000)


class SetRequest(BaseModel):
    exercise_id: int = Field(..., ge=1)
    reps: int = Field(..., ge=1, le=100)
    weight_kg: float = Field(0, ge=0, le=1000)
    set_order: int | None = Field(None, ge=1, le=20)
    set_type: str = Field("working", max_length=40)
    rpe: float | None = Field(None, ge=0, le=10)
    rir: float | None = Field(None, ge=0, le=10)
    rest_seconds: int | None = Field(None, ge=0, le=900)
    is_warmup: bool = False
    note: str = Field("", max_length=500)


class MeasurementRequest(BaseModel):
    kind: str = Field(..., min_length=1, max_length=40)
    value: float = Field(..., ge=0, le=1000)
    unit: str | None = Field(None, max_length=20)
    measured_at: str | None = Field(None, max_length=80)
    note: str = Field("", max_length=500)


def _uid(request: Request) -> str:
    auth = require_roles(request, "owner", "internal")
    return str(auth.subject or owner_user_id())


def _raise_domain(exc: Exception) -> None:
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail=str(exc.args[0] if exc.args else "资源不存在")) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail="健身请求无法处理") from exc


def _plan_payload(req: PlanRequest) -> dict[str, Any]:
    return req.model_dump(exclude_none=True)


@router.get("/fitness/profile")
async def get_fitness_profile(request: Request) -> dict[str, Any]:
    uid = _uid(request)
    return {"profile": await asyncio.to_thread(fitness_training.get_profile, uid)}


@router.put("/fitness/profile")
async def put_fitness_profile(req: ProfileRequest, request: Request) -> dict[str, Any]:
    uid = _uid(request)
    try:
        profile = await asyncio.to_thread(fitness_training.upsert_profile, uid, req.model_dump())
    except (KeyError, ValueError) as exc:
        _raise_domain(exc)
    return {"profile": profile}


@router.get("/fitness/summary")
async def fitness_summary(request: Request, days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    uid = _uid(request)
    return await asyncio.to_thread(fitness_training.get_summary, uid, days=days)


@router.get("/fitness/exercises")
async def list_fitness_exercises(
    request: Request,
    q: str = Query("", max_length=160),
    muscle: str = Query("", max_length=80),
    equipment: str = Query("", max_length=80),
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    _uid(request)
    exercises = await asyncio.to_thread(
        fitness_catalog.list_exercises,
        query=q,
        muscle=muscle,
        equipment=equipment,
        limit=limit,
    )
    return {"query": q, "muscle": muscle, "equipment": equipment, "results": exercises}


@router.get("/fitness/exercises/{exercise_id}")
async def get_fitness_exercise(exercise_id: int, request: Request) -> dict[str, Any]:
    _uid(request)
    exercise = await asyncio.to_thread(fitness_catalog.get_exercise, exercise_id)
    if not exercise:
        raise HTTPException(status_code=404, detail="动作不存在")
    return exercise


@router.post("/fitness/catalog/import")
async def import_fitness_catalog(req: ImportRequest, request: Request) -> dict[str, Any]:
    uid = _uid(request)
    if not req.records:
        raise HTTPException(status_code=422, detail="records 不能为空")
    try:
        result = await asyncio.to_thread(
            fitness_catalog.import_exercises,
            req.records,
            source=req.source,
            license_name=req.license_name,
            attribution=req.attribution,
        )
        audit = await asyncio.to_thread(
            fitness_catalog.record_import,
            uid,
            source=req.source,
            external_id=req.external_id,
            content_hash=fitness_catalog.content_hash(req.records),
            status="imported",
            imported_count=result["total"],
        )
    except (KeyError, ValueError) as exc:
        _raise_domain(exc)
    return {**result, "import_id": audit["id"]}


@router.post("/fitness/imports/preview")
async def preview_fitness_import(req: PreviewRequest, request: Request) -> dict[str, Any]:
    _uid(request)
    if not req.records:
        raise HTTPException(status_code=422, detail="records 不能为空")
    try:
        normalized = [
            fitness_catalog.normalize_exercise_record(
                item,
                source=req.source,
                license_name=req.license_name,
                attribution=req.attribution,
            )
            for item in req.records
        ]
    except ValueError as exc:
        _raise_domain(exc)
    return {
        "source": req.source,
        "count": len(normalized),
        "content_hash": fitness_catalog.content_hash(normalized),
        "preview": normalized[:10],
    }


@router.get("/fitness/foods")
async def list_fitness_foods(
    request: Request,
    q: str = Query("", max_length=200),
    brand: str = Query("", max_length=160),
    source: str = Query("", max_length=80),
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    _uid(request)
    results = await asyncio.to_thread(
        fitness_nutrition.list_foods,
        query=q,
        brand=brand,
        source=source,
        limit=limit,
    )
    return {"query": q, "brand": brand, "source": source, "results": results}


@router.get("/fitness/foods/{food_id}")
async def get_fitness_food(food_id: int, request: Request) -> dict[str, Any]:
    _uid(request)
    food = await asyncio.to_thread(fitness_nutrition.get_food, food_id)
    if not food:
        raise HTTPException(status_code=404, detail="食品不存在")
    return food


@router.post("/fitness/foods/import")
async def import_fitness_foods(req: FoodImportRequest, request: Request) -> dict[str, Any]:
    uid = _uid(request)
    if not req.records:
        raise HTTPException(status_code=422, detail="records 不能为空")
    try:
        result = await asyncio.to_thread(
            fitness_nutrition.import_foods,
            req.records,
            source=req.source,
            license_name=req.license_name,
            attribution=req.attribution,
            skip_invalid=req.skip_invalid,
        )
        audit = await asyncio.to_thread(
            fitness_catalog.record_import,
            uid,
            source=req.source,
            external_id=None,
            content_hash=fitness_catalog.content_hash(req.records),
            status="imported",
            imported_count=result["imported"],
        )
    except (KeyError, ValueError) as exc:
        _raise_domain(exc)
    return {**result, "import_id": audit["id"]}


@router.post("/fitness/nutrition/logs")
async def create_fitness_nutrition_log(req: NutritionLogRequest, request: Request) -> dict[str, Any]:
    uid = _uid(request)
    try:
        result = await asyncio.to_thread(
            fitness_nutrition.log_food,
            uid,
            req.food_id,
            **req.model_dump(exclude={"food_id"}, exclude_none=True),
        )
    except (KeyError, ValueError) as exc:
        _raise_domain(exc)
    return result


@router.get("/fitness/nutrition/logs")
async def list_fitness_nutrition_logs(
    request: Request,
    date: str | None = Query(None, max_length=10),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    uid = _uid(request)
    try:
        results = await asyncio.to_thread(fitness_nutrition.list_food_logs, uid, date=date, limit=limit)
    except ValueError as exc:
        _raise_domain(exc)
    return {"date": date, "results": results}


@router.get("/fitness/nutrition/summary")
async def fitness_nutrition_summary(
    request: Request,
    date: str | None = Query(None, max_length=10),
) -> dict[str, Any]:
    uid = _uid(request)
    try:
        return await asyncio.to_thread(fitness_nutrition.nutrition_summary, uid, date=date)
    except ValueError as exc:
        _raise_domain(exc)


@router.delete("/fitness/nutrition/logs/{log_id}")
async def delete_fitness_nutrition_log(log_id: int, request: Request) -> dict[str, Any]:
    uid = _uid(request)
    try:
        await asyncio.to_thread(fitness_nutrition.delete_food_log, uid, log_id)
    except (KeyError, ValueError) as exc:
        _raise_domain(exc)
    return {"deleted": True, "id": log_id}


@router.get("/fitness/plans")
async def list_fitness_plans(
    request: Request,
    status: str | None = Query(None),
    limit: int = Query(20, ge=1, le=50),
) -> dict[str, Any]:
    uid = _uid(request)
    try:
        plans = await asyncio.to_thread(fitness_training.list_plans, uid, status=status, limit=limit)
    except ValueError as exc:
        _raise_domain(exc)
    return {"results": plans}


@router.get("/fitness/plans/{plan_id}")
async def get_fitness_plan(plan_id: int, request: Request) -> dict[str, Any]:
    uid = _uid(request)
    plan = await asyncio.to_thread(fitness_training.get_plan, uid, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="计划不存在")
    return plan


@router.post("/fitness/plans/generate")
async def generate_fitness_plan(req: GeneratePlanRequest, request: Request) -> dict[str, Any]:
    uid = _uid(request)
    try:
        return await fitness_coach.generate_plan_draft(
            uid,
            req.model_dump(),
            request_id=getattr(request.state, "request_id", None),
        )
    except (KeyError, ValueError, RuntimeError) as exc:
        _raise_domain(exc)


@router.post("/fitness/plans")
async def create_fitness_plan(req: PlanRequest, request: Request) -> dict[str, Any]:
    uid = _uid(request)
    try:
        plan = await asyncio.to_thread(fitness_training.create_plan, uid, _plan_payload(req))
    except (KeyError, ValueError) as exc:
        _raise_domain(exc)
    return plan


@router.post("/fitness/plans/{plan_id}/activate")
async def activate_fitness_plan(plan_id: int, request: Request) -> dict[str, Any]:
    uid = _uid(request)
    try:
        return await asyncio.to_thread(fitness_training.activate_plan, uid, plan_id)
    except (KeyError, ValueError) as exc:
        _raise_domain(exc)


@router.post("/fitness/plans/{plan_id}/archive")
async def archive_fitness_plan(plan_id: int, request: Request) -> dict[str, Any]:
    uid = _uid(request)
    try:
        return await asyncio.to_thread(fitness_training.archive_plan, uid, plan_id)
    except (KeyError, ValueError) as exc:
        _raise_domain(exc)


@router.get("/fitness/active-plan")
async def active_fitness_plan(request: Request) -> dict[str, Any]:
    uid = _uid(request)
    return {"plan": await asyncio.to_thread(fitness_training.get_active_plan, uid)}


@router.post("/fitness/sessions")
async def start_fitness_session(req: SessionRequest, request: Request) -> dict[str, Any]:
    uid = _uid(request)
    try:
        return await asyncio.to_thread(fitness_training.start_session, uid, **req.model_dump(exclude_none=True))
    except (KeyError, ValueError) as exc:
        _raise_domain(exc)


@router.get("/fitness/sessions")
async def list_fitness_sessions(
    request: Request,
    status: str | None = Query(None),
    limit: int = Query(20, ge=1, le=50),
) -> dict[str, Any]:
    uid = _uid(request)
    try:
        sessions = await asyncio.to_thread(fitness_training.list_sessions, uid, status=status, limit=limit)
    except ValueError as exc:
        _raise_domain(exc)
    return {"results": sessions}


@router.get("/fitness/sessions/{session_id}")
async def get_fitness_session(session_id: int, request: Request) -> dict[str, Any]:
    uid = _uid(request)
    session = await asyncio.to_thread(fitness_training.get_session, uid, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="训练会话不存在")
    return session


@router.post("/fitness/sessions/{session_id}/sets")
async def log_fitness_set(session_id: int, req: SetRequest, request: Request) -> dict[str, Any]:
    uid = _uid(request)
    try:
        return await asyncio.to_thread(
            fitness_training.log_set,
            uid,
            session_id,
            req.exercise_id,
            **req.model_dump(exclude={"exercise_id"}, exclude_none=True),
        )
    except (KeyError, ValueError) as exc:
        _raise_domain(exc)


@router.post("/fitness/sessions/{session_id}/complete")
async def complete_fitness_session(session_id: int, request: Request) -> dict[str, Any]:
    uid = _uid(request)
    try:
        return await asyncio.to_thread(fitness_training.complete_session, uid, session_id)
    except (KeyError, ValueError) as exc:
        _raise_domain(exc)


@router.post("/fitness/measurements")
async def record_fitness_measurement(req: MeasurementRequest, request: Request) -> dict[str, Any]:
    uid = _uid(request)
    try:
        return await asyncio.to_thread(
            fitness_training.record_measurement,
            uid,
            **req.model_dump(exclude_none=True),
        )
    except (KeyError, ValueError) as exc:
        _raise_domain(exc)


@router.get("/fitness/measurements")
async def list_fitness_measurements(
    request: Request,
    kind: str | None = Query(None),
    limit: int = Query(30, ge=1, le=100),
) -> dict[str, Any]:
    uid = _uid(request)
    try:
        results = await asyncio.to_thread(fitness_training.list_measurements, uid, kind=kind, limit=limit)
    except ValueError as exc:
        _raise_domain(exc)
    return {"results": results}
