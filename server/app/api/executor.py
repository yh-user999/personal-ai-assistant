"""执行器 API：入队 / 轮询 / 回传。鉴权由全局中间件统一处理。"""
import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.auth import require_roles
from app.core import memory
from app.services import executor

router = APIRouter()

# 远程允许的指令集合。run_script 不在其中——远程跑脚本属安全分级③，
# 只允许桌面本地执行器解析。
ALLOWED_ACTIONS = {"open", "list_dir", "read_file", "copy", "backup", "move", "rename", "search_files"}

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
    claim_token: str = ""
    device_id: str = ""


@router.post("/executor/enqueue")
async def enqueue(req: EnqueueRequest, request: Request) -> dict:
    require_roles(request, "owner", "internal")
    """入队。白名单在此强制执行——聊天解析与 API 直调两条入口都受控。"""
    if req.action not in ALLOWED_ACTIONS:
        raise HTTPException(status_code=400, detail=f"不支持的指令类型：{req.action}")
    # 所有涉及本机路径的动作都必须经过白名单；远程 open 不再享受黑名单豁免。
    # 这样 API token 泄露也不能启动任意 exe/lnk/url。
    paths = executor.unpack_paths(req.action, req.target)
    if req.action == "open":
        if not executor.check_open_target(req.target):
            raise HTTPException(status_code=400, detail="打开目标不在白名单或不是已登记别名")
    elif (req.action != "search_files" or paths) and (
        not paths or not all(executor.check_roots(p) for p in paths)
    ):
        raise HTTPException(
            status_code=400, detail="目标路径超出白名单（EXECUTOR_ALLOWED_ROOTS）"
        )
    cmd_id = executor.enqueue(req.action, req.target)
    return {"id": cmd_id}


@router.get("/executor/pending")
async def pending(request: Request) -> dict:
    require_roles(request, "executor", "internal", "owner")
    device_id = request.headers.get("X-Executor-Device", "")[:100]
    cmd = executor.get_pending(device_id)
    return {"command": cmd}  # null = 无待执行


@router.get("/executor/results")
async def results(request: Request, since_id: int = 0) -> dict:
    require_roles(request, "executor", "internal", "owner")
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
async def result(req: ResultRequest, request: Request) -> dict:
    require_roles(request, "executor", "internal", "owner")
    device_id = req.device_id or request.headers.get("X-Executor-Device", "")
    accepted = executor.mark_result(req.id, req.ok, req.result, req.claim_token, device_id)
    if not accepted:
        raise HTTPException(status_code=409, detail="指令不存在、未认领或结果已回传")
    # 结果写为 assistant 消息，用户下次聊天/看历史可见
    if req.result:
        text = f"[执行结果] {req.result}" if req.ok else f"[执行失败] {req.result}"
        _spawn(memory.write_message("assistant", text))
    return {"ok": True}
