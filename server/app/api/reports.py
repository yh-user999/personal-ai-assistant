"""周报接口：查询历史周报 / 手动触发生成。"""
import asyncio

from fastapi import APIRouter, Request

from app.auth import require_roles
from app.core.memory import owner_user_id
from app.models import repo

router = APIRouter()


def _subject(request: Request) -> str:
    auth = require_roles(request, "owner", "internal")
    return str(auth.subject or owner_user_id())


@router.get("/reports")
async def list_reports(request: Request) -> dict:
    uid = _subject(request)
    return {"reports": await asyncio.to_thread(repo.list_weekly_reports, uid)}


@router.get("/reports/{week}")
async def get_report(week: str, request: Request) -> dict:
    uid = _subject(request)
    row = await asyncio.to_thread(repo.get_weekly_report, uid, week)
    if not row:
        return {"error": "not found"}
    return row


@router.post("/reports/generate")
async def generate_now(request: Request) -> dict:
    """手动触发本周反思（调试用）。"""
    from app.services.weekly_reflect import run_weekly_reflect

    uid = _subject(request)
    request_id = request.headers.get("x-request-id", "")
    return await run_weekly_reflect(user_id=uid, request_id=request_id)


@router.get("/daily/latest")
async def latest_daily(request: Request) -> dict:
    """最新每日小结（桌面托盘检查用）。"""
    uid = _subject(request)
    row = await asyncio.to_thread(repo.latest_daily_summary, uid)
    if not row:
        return {"exists": False}
    return {"exists": True, **row}
