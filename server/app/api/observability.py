"""只读可观测 API：Trace 列表、单条详情和检索聚合摘要。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.auth import require_roles
from app.core.memory import owner_user_id
from app.services import observability

router = APIRouter()


def _subject(request: Request) -> str:
    auth = require_roles(request, "owner", "internal")
    return str(auth.subject or owner_user_id())


@router.get("/observability/traces")
async def list_observability_traces(
    request: Request,
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(50, ge=1, le=observability.MAX_LIMIT),
    offset: int = Query(0, ge=0, le=observability.MAX_OFFSET),
    trace_id: str = Query("", max_length=160),
    request_id: str = Query("", max_length=160),
    q: str = Query("", max_length=200),
    status: str = Query("", max_length=32),
    channel: str = Query("", max_length=40),
    retrieval_path: str = Query("", max_length=40),
) -> dict:
    uid = _subject(request)
    return await asyncio.to_thread(
        observability.list_traces,
        uid,
        days=days,
        limit=limit,
        offset=offset,
        trace_id=trace_id,
        request_id=request_id,
        query=q,
        status=status,
        channel=channel,
        retrieval_path=retrieval_path,
    )


@router.get("/observability/traces/{trace_id}")
async def get_observability_trace(request: Request, trace_id: str) -> dict:
    uid = _subject(request)
    item = await asyncio.to_thread(observability.get_trace, uid, trace_id)
    if item is None:
        return JSONResponse({"detail": "trace_not_found"}, status_code=404)
    return item


@router.get("/retrieval/debug")
async def retrieval_debug(
    request: Request,
    days: int = Query(7, ge=1, le=90),
) -> dict:
    """检索质量摘要；默认只返回聚合值，不暴露查询原文。"""
    uid = _subject(request)
    summary = await asyncio.to_thread(observability.summary, uid, days=days)
    return {"scope": "owner", "summary": summary}
