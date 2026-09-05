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


async def send_private(text: str) -> bool:
    """给主人发一条 QQ 私聊，返回是否确认送达。所有 QQ 推送的单一出口。

    收敢动因：原先有三处各自实现（本模块的提醒推送、initiative 的主动开口、
    scheduler 的任务失败告警），且后两处每次调用都新建 httpx.AsyncClient
    ——连接不复用，而且"成功"的判据散在三处（LESSONS 第 16 条：同一判据出现
    在多个文件里等于三个都不可信）。
    """
    if not (settings.qq_push_url and settings.qq_admin_id):
        return False
    headers = (
        {"Authorization": f"Bearer {settings.qq_push_token}"}
        if settings.qq_push_token
        else {}
    )
    try:
        r = await _get_client().post(
            f"{settings.qq_push_url.rstrip('/')}/send_private_msg",
            json={"user_id": int(settings.qq_admin_id), "message": text},
            headers=headers,
        )
    except (httpx.HTTPError, ValueError, TypeError) as e:
        logger.warning("QQ 推送异常: %s", e)
        return False
    if r.status_code == 200 and r.json().get("status") == "ok":
        return True
    logger.warning("QQ 推送失败: HTTP %s %s", r.status_code, r.text[:120])
    return False


async def push_reminders() -> int:
    """推送所有到期提醒给主人 QQ，返回被成功消费的提醒条数。"""
    if not (settings.qq_push_url and settings.qq_admin_id):
        return 0
    from app.core.memory import owner_user_id

    owner = owner_user_id()
    items = reminders.claim_due_reminders(user_id=owner)
    if not items:
        return 0

    token = items[0].get("sending_token") or ""
    stale = [item for item in items if item.get("stale")]
    fresh = [item for item in items if not item.get("stale")]
    consumed = 0

    # 长期掉线后的积压只发一条摘要；摘要成功后直接消费同一 claim token，
    # 不再把 stale 项加入逐条发送列表，避免摘要后再次轰炸。
    if stale:
        digest = "\n".join(f"· {item['content']}" for item in stale)
        summary = (
            f"📋 {len(stale)} 条过期提醒（摘要合并）：\n{digest}\n"
            "（已超 24 小时，如仍需要请重新设置）"
        )
        if await send_private(summary):
            stale_ids = [item["id"] for item in stale]
            await asyncio.to_thread(reminders.mark_notified, stale_ids, token, owner)
            consumed += len(stale_ids)
        else:
            await asyncio.to_thread(reminders.release_claim, [item["id"] for item in stale], token, owner)

    succeeded: list[int] = []
    failed: list[int] = []
    for item in fresh:
        if await send_private(f"⏰ 提醒：{item['content']}"):
            succeeded.append(item["id"])
        else:
            failed.append(item["id"])
    if succeeded:
        await asyncio.to_thread(reminders.mark_notified, succeeded, token, owner)
        consumed += len(succeeded)
    if failed:
        await asyncio.to_thread(reminders.release_claim, failed, token, owner)
    return consumed
