"""执行器 API：入队 / 轮询 / 回传。鉴权由全局中间件统一处理。"""
from fastapi import APIRouter
from pydantic import BaseModel

from app.services import executor

router = APIRouter()


class EnqueueRequest(BaseModel):
    action: str
    target: str


class ResultRequest(BaseModel):
    id: int
    ok: bool
    result: str = ""


@router.post("/executor/enqueue")
async def enqueue(req: EnqueueRequest) -> dict:
    cmd_id = executor.enqueue(req.action, req.target)
    return {"id": cmd_id}


@router.get("/executor/pending")
async def pending() -> dict:
    cmd = executor.get_pending()
    return {"command": cmd}  # null = 无待执行


@router.get("/executor/results")
async def results(since_id: int = 0) -> dict:
    """id > since_id 的已执行指令（桌面端轮询显示执行结果）。"""
    from app.models.database import connect

    conn = connect()
    try:
        rows = conn.execute(
            """SELECT id, action, target, status, result FROM executor_commands
               WHERE id > ? AND status != 'pending' ORDER BY id""",
            (since_id,),
        ).fetchall()
    finally:
        conn.close()
    return {"results": [dict(r) for r in rows]}


@router.post("/executor/result")
async def result(req: ResultRequest) -> dict:
    executor.mark_result(req.id, req.ok, req.result)
    # 结果写为 assistant 消息，用户下次聊天/看历史可见
    if req.result:
        from app.core import memory
        import asyncio

        asyncio.create_task(
            memory.write_message(
                "assistant", f"[执行结果] {req.result}" if req.ok else f"[执行失败] {req.result}"
            )
        )
    return {"ok": True}
