"""QQ 提醒推送（第 8 课）：到期提醒的唯一通道。

服务器主动推 QQ（手机必达，不再依赖电脑开机）。走 NapCat onebot HTTP：
POST {qq_push_url}/send_private_msg。取即消费复用 reminders.due_reminders()
的幂等语义，轮询丢了只是少推一次，不会重复轰炸。
"""
import logging

import httpx

from app.config import settings
from app.services import reminders

logger = logging.getLogger("assistant.qq_push")


async def push_reminders() -> int:
    """推送所有到期提醒给主人 QQ，返回成功推送条数。未配置通道返回 0。"""
    if not (settings.qq_push_url and settings.qq_admin_id):
        return 0
    items = reminders.due_reminders()
    if not items:
        return 0
    headers = (
        {"Authorization": f"Bearer {settings.qq_push_token}"}
        if settings.qq_push_token
        else {}
    )
    pushed = 0
    async with httpx.AsyncClient(timeout=8) as client:
        for item in items:
            try:
                r = await client.post(
                    f"{settings.qq_push_url.rstrip('/')}/send_private_msg",
                    json={
                        "user_id": int(settings.qq_admin_id),
                        "message": f"⏰ 提醒：{item['content']}",
                    },
                    headers=headers,
                )
                if r.status_code == 200 and r.json().get("status") == "ok":
                    pushed += 1
                else:
                    logger.warning("QQ 提醒推送失败 #%s: HTTP %s %s", item["id"], r.status_code, r.text[:120])
            except Exception as e:
                logger.warning("QQ 提醒推送异常 #%s: %s", item["id"], e)
    return pushed
