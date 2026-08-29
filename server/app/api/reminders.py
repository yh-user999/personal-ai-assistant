"""提醒 API：桌面机器人轮询到期提醒（30 秒一次），取到即标记已推送。

第 6.24 课。推送闭环：用户聊天设提醒 → 机器人轮询 /reminders/due →
托盘弹窗通知 → 下一条对话里也可以自然提起。鉴权由全局中间件统一处理。
"""
from fastapi import APIRouter

from app.services import reminders

router = APIRouter()


@router.get("/reminders/due")
async def due() -> dict:
    items = reminders.due_reminders()
    return {"reminders": items}
