"""周报接口：查询历史周报 / 手动触发生成。"""
from fastapi import APIRouter, Request

from app.auth import require_roles
from app.core.memory import _user_scope, owner_user_id
from app.models.database import connect

router = APIRouter()


def _subject(request: Request) -> str:
    auth = require_roles(request, "owner", "internal")
    return str(auth.subject or owner_user_id())


@router.get("/reports")
async def list_reports(request: Request) -> dict:
    uid = _subject(request)
    clause, args = _user_scope(uid)
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT week, created_at FROM weekly_reports WHERE {clause} "
            "ORDER BY week DESC, id DESC LIMIT 20",
            args,
        ).fetchall()
    finally:
        conn.close()
    return {"reports": [dict(r) for r in rows]}


@router.get("/reports/{week}")
async def get_report(week: str, request: Request) -> dict:
    uid = _subject(request)
    clause, args = _user_scope(uid)
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT week, content, stats, created_at FROM weekly_reports "
            f"WHERE week=? AND {clause}",
            (week, *args),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"error": "not found"}
    return dict(row)


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
    clause, args = _user_scope(uid)
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT date, content, created_at FROM daily_summaries WHERE {clause} "
            "ORDER BY date DESC, id DESC LIMIT 1",
            args,
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"exists": False}
    return {"exists": True, **dict(row)}
