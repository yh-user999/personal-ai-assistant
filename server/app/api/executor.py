"""执行器 API：入队 / 轮询 / 回传。鉴权由全局中间件统一处理。"""
import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core import memory
from app.services import executor

router = APIRouter()

# 远程允许的指令集合。run_script 不在其中——远程跑脚本属安全分级③，
# 只允许桌面本地执行器解析。
ALLOWED_ACTIONS = {"open", "list_dir", "read_file", "copy", "backup", "move", "rename"}

# 火后不管任务须保留引用，否则可能被 GC 中途回收、结果丢失
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


class EnqueueRequest(BaseModel):
    action: str
    target: str


class ResultRequest(BaseModel):
    id: int
    ok: bool
    result: str = ""


@router.post("/executor/enqueue")
async def enqueue(req: EnqueueRequest) -> dict:
    """入队。白名单在此强制执行——聊天解析与 API 直调两条入口都受控。"""
    if req.action not in ALLOWED_ACTIONS:
        raise HTTPException(status_code=400, detail=f"不支持的指令类型：{req.action}")
    if req.action != "open":
        paths = executor.unpack_paths(req.action, req.target)
        if not paths or not all(executor.check_roots(p) for p in paths):
            raise HTTPException(
                status_code=400, detail="目标路径超出白名单（EXECUTOR_ALLOWED_ROOTS）"
            )
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
               WHERE id > ? AND status IN ('done', 'failed') ORDER BY id""",
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
        text = f"[执行结果] {req.result}" if req.ok else f"[执行失败] {req.result}"
        _spawn(memory.write_message("assistant", text))
    return {"ok": True}
