"""提醒 API：只读预览与显式原子领取分离，避免 GET 产生副作用。"""
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.auth import require_roles
from app.core.memory import owner_user_id
from app.services import reminders

router = APIRouter()


class ClaimRequest(BaseModel):
    stale_hours: float = Field(24.0, ge=0.0, le=24 * 30)
    limit: int = Field(10, ge=1, le=100)


def _subject(request: Request) -> str:
    auth = require_roles(request, "owner", "internal")
    return str(auth.subject or owner_user_id())


@router.get("/reminders/due")
async def due(request: Request) -> dict:
    """只读查询当前认证主体的到期提醒，不改变状态。"""
    return {"reminders": reminders.peek_due_reminders(user_id=_subject(request))}


@router.post("/reminders/claim")
async def claim(request: Request, req: ClaimRequest | None = None) -> dict:
    """显式原子领取当前主体的到期提醒，token 只由服务端生成。"""
    req = req or ClaimRequest()
    items = reminders.claim_due_reminders(
        stale_hours=req.stale_hours,
        # 不接受请求体提供的 token，避免外部复用/覆盖内部 lease。
        claim_token=None,
        limit=req.limit,
        user_id=_subject(request),
    )
    return {"reminders": items}
