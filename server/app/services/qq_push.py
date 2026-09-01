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
    except Exception as e:
        logger.warning("QQ 推送异常: %s", e)
        return False
    if r.status_code == 200 and r.json().get("status") == "ok":
        return True
    logger.warning("QQ 推送失败: HTTP %s %s", r.status_code, r.text[:120])
    return False


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
    client = _get_client()

    # NapCat 长期掉线恢复后的积压防轰炸：超 24h 的老项合并成一条摘要推送
    fresh = [it for it in items if not it.get("stale")]
    stale = [it for it in items if it.get("stale")]
    if stale:
        digest = "\n".join(f"· {it['content']}" for it in stale)
        try:
            r = await client.post(
                f"{settings.qq_push_url.rstrip('/')}/send_private_msg",
                json={
                    "user_id": int(settings.qq_admin_id),
                    "message": f"📋 {len(stale)} 条过期提醒（摘要合并）：\n{digest}\n（已超 24 小时，如仍需要请重新设置）",
                },
                headers=headers,
            )
            if r.status_code == 200 and r.json().get("status") == "ok":
                fresh.extend(stale)  # 摘要送达即消费，不再逐条轰炸
            else:
                logger.warning("积压提醒摘要推送失败: HTTP %s", r.status_code)
        except Exception as e:
            logger.warning("积压提醒摘要推送异常: %s", e)

    pushed_ids: list[int] = []
    for item in fresh:
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
