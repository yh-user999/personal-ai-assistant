"""每日 AI 资讯日报 API。"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from app.auth import require_roles
from app.identity import owner_user_id

from . import service

router = APIRouter()


class GenerateRequest(BaseModel):
    force: bool = False
    request_id: str = Field("", max_length=160)


def _subject(request: Request) -> str:
    auth = require_roles(request, "owner", "internal")
    return str(auth.subject or owner_user_id())


@router.get("/ai-news/latest")
async def latest(request: Request) -> dict[str, Any]:
    uid = _subject(request)
    row = await asyncio.to_thread(service.latest_digest, uid)
    return {"exists": bool(row), **(row or {})}


@router.get("/ai-news")
async def history(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    uid = _subject(request)
    rows = await asyncio.to_thread(service.list_digests, uid, limit)
    return {"digests": rows}


@router.post("/ai-news/generate")
async def generate(payload: GenerateRequest, request: Request) -> dict[str, Any]:
    uid = _subject(request)
    request_id = payload.request_id or request.headers.get("x-request-id", "")
    return await service.run_daily_ai_news_digest(
        user_id=uid,
        request_id=request_id or None,
        force=payload.force,
    )


__all__ = ["router"]
