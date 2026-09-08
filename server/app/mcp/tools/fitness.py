"""私人 MCP 的健身工具：读取默认开放，写入必须显式确认。"""
from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.context import Context

from app.services import fitness_catalog, fitness_training

from ..audit import audited_tool
from ..permissions import require_confirmed_action, require_read
from ..schemas import bounded_limit, cap_payload
from .common import text


@audited_tool
async def get_fitness_summary(
    days: int = 30,
    ctx: Context | None = None,
) -> dict[str, Any]:
    identity = require_read(ctx)
    days = max(1, min(int(days), 365))
    return cap_payload(await _to_thread(fitness_training.get_summary, identity.uid, days=days))


@audited_tool
async def search_fitness_exercises(
    query: str = "",
    muscle: str = "",
    equipment: str = "",
    limit: int = 20,
    ctx: Context | None = None,
) -> dict[str, Any]:
    require_read(ctx)
    query = text(query or "", "query", max_chars=160) if query else ""
    muscle = text(muscle or "", "muscle", max_chars=80) if muscle else ""
    equipment = text(equipment or "", "equipment", max_chars=80) if equipment else ""
    limit = bounded_limit(limit, name="limit", default=20, maximum=50)
    rows = await _to_thread(
        fitness_catalog.list_exercises,
        query=query,
        muscle=muscle,
        equipment=equipment,
        limit=limit,
    )
    return cap_payload({"query": query, "muscle": muscle, "equipment": equipment, "results": rows})


@audited_tool
async def get_active_fitness_plan(ctx: Context | None = None) -> dict[str, Any]:
    identity = require_read(ctx)
    return cap_payload({"plan": await _to_thread(fitness_training.get_active_plan, identity.uid)})


@audited_tool
async def list_fitness_sessions(
    status: str | None = None,
    limit: int = 20,
    ctx: Context | None = None,
) -> dict[str, Any]:
    identity = require_read(ctx)
    limit = bounded_limit(limit, name="limit", default=20, maximum=50)
    rows = await _to_thread(fitness_training.list_sessions, identity.uid, status=status, limit=limit)
    return cap_payload({"status": status, "limit": limit, "results": rows})


@audited_tool
async def record_fitness_measurement(
    kind: str,
    value: float,
    unit: str | None = None,
    measured_at: str | None = None,
    note: str = "",
    confirmed: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    identity = require_confirmed_action(ctx, confirmed=confirmed)
    result = await _to_thread(
        fitness_training.record_measurement,
        identity.uid,
        kind=text(kind, "kind", max_chars=40),
        value=value,
        unit=text(unit, "unit", max_chars=20) if unit else None,
        measured_at=text(measured_at, "measured_at", max_chars=80) if measured_at else None,
        note=text(note, "note", max_chars=500) if note else "",
    )
    return cap_payload({"recorded": True, "measurement": result})


@audited_tool
async def start_fitness_session(
    plan_id: int | None = None,
    plan_day_id: int | None = None,
    source: str = "local",
    external_id: str | None = None,
    notes: str = "",
    confirmed: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    identity = require_confirmed_action(ctx, confirmed=confirmed)
    result = await _to_thread(
        fitness_training.start_session,
        identity.uid,
        plan_id=plan_id,
        plan_day_id=plan_day_id,
        source=text(source, "source", max_chars=80),
        external_id=text(external_id, "external_id", max_chars=200) if external_id else None,
        notes=text(notes, "notes", max_chars=1000) if notes else "",
    )
    return cap_payload({"started": True, "session": result})


@audited_tool
async def log_fitness_set(
    session_id: int,
    exercise_id: int,
    reps: int,
    weight_kg: float = 0,
    set_order: int | None = None,
    set_type: str = "working",
    rpe: float | None = None,
    rir: float | None = None,
    rest_seconds: int | None = None,
    is_warmup: bool = False,
    note: str = "",
    confirmed: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    identity = require_confirmed_action(ctx, confirmed=confirmed)
    result = await _to_thread(
        fitness_training.log_set,
        identity.uid,
        session_id,
        exercise_id,
        reps=reps,
        weight_kg=weight_kg,
        set_order=set_order,
        set_type=text(set_type, "set_type", max_chars=40),
        rpe=rpe,
        rir=rir,
        rest_seconds=rest_seconds,
        is_warmup=is_warmup,
        note=text(note, "note", max_chars=500) if note else "",
    )
    return cap_payload({"recorded": True, "set": result})


@audited_tool
async def complete_fitness_session(
    session_id: int,
    confirmed: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    identity = require_confirmed_action(ctx, confirmed=confirmed)
    result = await _to_thread(fitness_training.complete_session, identity.uid, session_id)
    return cap_payload({"completed": True, "session": result})


async def _to_thread(func, *args, **kwargs):
    import asyncio

    return await asyncio.to_thread(func, *args, **kwargs)
