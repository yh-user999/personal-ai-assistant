"""行为事件接收接口：Windows 采集器推送的事件入库 + 心跳上报。"""
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.models.database import connect

router = APIRouter()


class BehaviorEvent(BaseModel):
    kind: str          # app_usage / browser / git_commit / manual
    name: str          # 应用名 / 域名 / 仓库名
    detail: str = ""
    start_ts: str = ""
    end_ts: str = ""
    meta: dict = {}


class EventBatch(BaseModel):
    events: list[BehaviorEvent]


@router.post("/events")
async def receive_events(batch: EventBatch) -> dict:
    """批量接收行为事件（采集器断网重试时也是整批推送）。

    幂等：按 (kind, name, detail, start_ts) 去重——同一事件重复推送只入库一次。
    场景：采集器换机器/换目录丢失游标后会补推历史，服务器必须能消化重复。
    鉴权：由全局 AuthMiddleware 统一处理（API_TOKEN）。
    脱敏：入库前统一过 sanitize（窗口标题里的公网 IP 打码，第 6.14 课）。
    """
    from app.services.sanitize import sanitize

    conn = connect()
    inserted = 0
    try:
        for e in batch.events:
            name = sanitize(e.name[:200])
            detail = sanitize(e.detail[:200])
            meta = sanitize(str(e.meta)[:500])
            dup = conn.execute(
                """SELECT 1 FROM behavior_events
                   WHERE kind=? AND name=? AND detail=? AND start_ts=? LIMIT 1""",
                (e.kind, name, detail, e.start_ts),
            ).fetchone()
            if dup:
                continue  # 重复事件跳过
            conn.execute(
                """INSERT INTO behavior_events (kind, name, detail, start_ts, end_ts, meta)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (e.kind, name, detail, e.start_ts, e.end_ts, meta),
            )
            inserted += 1
        conn.commit()
    finally:
        conn.close()
    return {"received": len(batch.events), "inserted": inserted}


class HeartbeatBody(BaseModel):
    client: str = "collector"
    channels: dict = {}  # 通道名 → 最近成功 ISO 时间


@router.post("/heartbeat")
async def heartbeat(body: HeartbeatBody, request: Request) -> dict:
    """采集器心跳：更新各通道最近成功时间，供健康检查检测采集停滞。

    心跳轻量存内存（app.state），服务重启即清零——个人场景足够。
    """
    request.app.state.collector_heartbeat = {
        "client": body.client,
        "channels": body.channels,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    return {"ok": True}
