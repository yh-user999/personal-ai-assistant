"""行为事件接收接口：Windows 采集器推送的事件入库。"""
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.config import settings
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
async def receive_events(batch: EventBatch, authorization: str | None = Header(default=None)) -> dict:
    """批量接收行为事件（采集器断网重试时也是整批推送）。"""
    if settings.collector_token:
        if authorization != f"Bearer {settings.collector_token}":
            raise HTTPException(status_code=401, detail="invalid token")

    conn = connect()
    try:
        for e in batch.events:
            conn.execute(
                """INSERT INTO behavior_events (kind, name, detail, start_ts, end_ts, meta)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (e.kind, e.name, e.detail[:200], e.start_ts, e.end_ts, str(e.meta)),
            )
        conn.commit()
    finally:
        conn.close()
    return {"received": len(batch.events)}
