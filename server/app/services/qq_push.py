"""QQ 提醒推送（第 8 课）：到期提醒的唯一通道。

服务器主动推 QQ（手机必达，不再依赖电脑开机）。走 NapCat onebot HTTP：
POST {qq_push_url}/send_private_msg。

可靠性语义（v0.3.1 修正）：先推送、成功后才 mark_notified 消费——
NapCat 掉线期间的到期提醒下一分钟重推，不会静默丢失（旧实现"选中即
标记、失败只告警"会永久吞掉提醒）。
"""
import asyncio
import logging

import httpx

from app.config import settings
from app.services import reminders

logger = logging.getLogger("assistant.qq_push")

_client: httpx.AsyncClient | None = None  # 每分钟调度复用，不逐轮重建


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=8)
    return _client


async def aclose() -> None:
    """关闭长驻客户端（进程退出前调用）。"""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def push_reminders() -> int:
    """推送所有到期提醒给主人 QQ，返回成功推送条数。未配置通道返回 0。

    qq_admin_id 配置成非数字会在启动期 fail-fast（见 config 校验），
    这里不再每分钟吞 ValueError。
    """
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
    pushed_ids: list[int] = []
    client = _get_client()
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
                pushed_ids.append(item["id"])
            else:
                logger.warning(
                    "QQ 提醒推送失败 #%s（下轮重推）: HTTP %s %s",
                    item["id"], r.status_code, r.text[:120],
                )
        except Exception as e:
            logger.warning("QQ 提醒推送异常 #%s（下轮重推）: %s", item["id"], e)
    if pushed_ids:
        # 只消费确认送达的；失败的留在 pending 下一分钟重推
        await asyncio.to_thread(reminders.mark_notified, pushed_ids)
    return len(pushed_ids)
