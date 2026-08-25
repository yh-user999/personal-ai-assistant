"""周报接口：查询历史周报 / 手动触发生成。"""
from fastapi import APIRouter

from app.models.database import connect

router = APIRouter()


@router.get("/reports")
async def list_reports() -> dict:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT week, created_at FROM weekly_reports ORDER BY week DESC LIMIT 20"
        ).fetchall()
    finally:
        conn.close()
    return {"reports": [dict(r) for r in rows]}


@router.get("/reports/{week}")
async def get_report(week: str) -> dict:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT week, content, stats, created_at FROM weekly_reports WHERE week=?", (week,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"error": "not found"}
    return dict(row)


@router.post("/reports/generate")
async def generate_now() -> dict:
    """手动触发本周反思（调试用）。"""
    from app.services.weekly_reflect import run_weekly_reflect

    return await run_weekly_reflect()


@router.get("/daily/latest")
async def latest_daily() -> dict:
    """最新每日小结（桌面托盘检查用）。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT date, content, created_at FROM daily_summaries ORDER BY date DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"exists": False}
    return {"exists": True, **dict(row)}
