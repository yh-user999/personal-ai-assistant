"""行为事件接收接口：Windows 采集器推送的事件入库 + 心跳上报。"""
import hashlib
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.auth import require_roles
from app.core.memory import owner_user_id
from app.models.database import connect

router = APIRouter()

MAX_META_BYTES = 4096


def _stable_event_id(*, kind: str, name: str, detail: str, start_ts: str, end_ts: str, meta: str) -> str:
    payload = {
        "kind": kind,
        "name": name,
        "detail": detail,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "meta": meta,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _normalize_meta(value: dict) -> str:
    """脱敏后仍保持合法 JSON，并限制落库大小。"""
    from app.services.sanitize import sanitize

    raw = json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    clean = sanitize(raw)
    if len(clean.encode("utf-8")) <= MAX_META_BYTES:
        return clean
    digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()
    return json.dumps(
        {"_truncated": True, "sha256": digest, "bytes": len(clean.encode("utf-8"))},
        ensure_ascii=False,
        separators=(",", ":"),
    )


class BehaviorEvent(BaseModel):
    event_id: str | None = Field(None, min_length=1, max_length=200)
    kind: str = Field(..., min_length=1, max_length=50)  # app_usage / browser / git_commit / manual
    name: str = Field(..., min_length=1, max_length=200)  # 应用名 / 域名 / 仓库名
    detail: str = Field("", max_length=500)
    start_ts: str = Field("", max_length=64)
    end_ts: str = Field("", max_length=64)
    meta: dict = Field(default_factory=dict, max_length=50)


class EventBatch(BaseModel):
    events: list[BehaviorEvent] = Field(default_factory=list, max_length=100)


@router.post("/events")
async def receive_events(batch: EventBatch, request: Request) -> dict:
    auth = require_roles(request, "collector", "internal", "owner")
    # collector/internal/owner 都是主人范围；事件主体只来自认证上下文，
    # 不信任批次中的任意 user_id 字段。
    user_id = str(auth.subject or owner_user_id()).strip() or owner_user_id()
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
            end_ts = e.end_ts[:64]
            start_ts = e.start_ts[:64]
            meta = _normalize_meta(e.meta)
            event_id = (e.event_id or "").strip() or _stable_event_id(
                kind=e.kind,
                name=name,
                detail=detail,
                start_ts=start_ts,
                end_ts=end_ts,
                meta=meta,
            )
            cur = conn.execute(
                """INSERT INTO behavior_events
                   (user_id, event_id, kind, name, detail, start_ts, end_ts, meta)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT DO NOTHING""",
                (user_id, event_id, e.kind, name, detail, start_ts, end_ts, meta),
            )
            inserted += max(0, cur.rowcount)
        conn.commit()
    finally:
        conn.close()
    return {"received": len(batch.events), "inserted": inserted}


class HeartbeatBody(BaseModel):
    client: str = Field("collector", min_length=1, max_length=50)
    channels: dict = Field(default_factory=dict, max_length=50)  # 通道名 → 最近成功 ISO 时间


@router.post("/heartbeat")
async def heartbeat(body: HeartbeatBody, request: Request) -> dict:
    require_roles(request, "collector", "internal", "owner")
    """采集器心跳：更新各通道最近成功时间，供健康检查检测采集停滞。

    心跳轻量存内存（app.state），服务重启即清零——个人场景足够。
    """
    request.app.state.collector_heartbeat = {
        "client": body.client,
        "channels": body.channels,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    return {"ok": True}
