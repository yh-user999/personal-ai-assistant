"""聊天请求的数据库级幂等层。

进程内单飞只解决同一进程的并发；本模块把 request_id 的占用、完成结果和租约
落到 SQLite，使跨线程、跨进程以及服务重启后的重试仍然不会重复执行聊天副作用。
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.models.database import connect

PROCESSING = "processing"
COMPLETED = "completed"
DEFAULT_LEASE_SECONDS = 120.0
DEFAULT_WAIT_SECONDS = 0.6
WAIT_INTERVAL_SECONDS = 0.05
COMPLETED_RETENTION_SECONDS = 24 * 60 * 60


class RequestDedupUnavailable(RuntimeError):
    """数据库尚未初始化幂等表；调用方可退回进程内兼容路径。"""


@dataclass(frozen=True)
class Claim:
    state: str
    lease_expires_at: str = ""
    response_json: str = ""

    @property
    def is_owner(self) -> bool:
        return self.state == "claimed"



def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _is_missing_table(exc: sqlite3.Error) -> bool:
    return "no such table: chat_request_dedup" in str(exc).casefold()


def _claim_once(
    user_id: str,
    request_id: str,
    request_hash: str,
    lease_seconds: float,
) -> Claim:
    conn = connect()
    now_dt = _utc_now()
    now = _iso(now_dt)
    lease = _iso(now_dt + timedelta(seconds=max(1.0, lease_seconds)))
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT request_hash, status, response_json, lease_expires_at "
            "FROM chat_request_dedup WHERE user_id=? AND request_id=?",
            (user_id, request_id),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO chat_request_dedup "
                "(user_id, request_id, request_hash, status, response_json, "
                "lease_expires_at, created_at, updated_at) "
                "VALUES (?, ?, ?, 'processing', '', ?, ?, ?)",
                (user_id, request_id, request_hash, lease, now, now),
            )
            conn.commit()
            return Claim("claimed", lease_expires_at=lease)

        if row["request_hash"] != request_hash:
            conn.rollback()
            return Claim("conflict")

        if row["status"] == COMPLETED:
            conn.commit()
            return Claim("completed", response_json=row["response_json"] or "")

        expires = row["lease_expires_at"] or ""
        if expires and expires > now:
            conn.commit()
            return Claim("processing")

        # 旧租约过期或异常为空：原子接管。把新的 lease 值当作本次所有权
        # 标记，complete/release 时必须带回，避免旧执行者覆盖新执行者结果。
        updated = conn.execute(
            "UPDATE chat_request_dedup SET status='processing', response_json='', "
            "lease_expires_at=?, updated_at=? WHERE user_id=? AND request_id=? "
            "AND request_hash=? AND status='processing'",
            (lease, now, user_id, request_id, request_hash),
        )
        if updated.rowcount != 1:
            conn.rollback()
            return Claim("processing")
        conn.commit()
        return Claim("claimed", lease_expires_at=lease)
    except sqlite3.Error as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        if _is_missing_table(exc):
            raise RequestDedupUnavailable from exc
        raise
    finally:
        conn.close()


def claim(
    user_id: str,
    request_id: str,
    request_hash: str,
    *,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> Claim:
    """尝试占用请求；结果为 claimed/completed/processing/conflict。"""
    return _claim_once(user_id, request_id, request_hash, lease_seconds)


def get_completed(user_id: str, request_id: str, request_hash: str) -> str | None:
    """读取同主体、同 request_hash 的已完成响应。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT request_hash, status, response_json FROM chat_request_dedup "
            "WHERE user_id=? AND request_id=?",
            (user_id, request_id),
        ).fetchone()
    except sqlite3.Error as exc:
        if _is_missing_table(exc):
            raise RequestDedupUnavailable from exc
        raise
    finally:
        conn.close()
    if row is None or row["request_hash"] != request_hash or row["status"] != COMPLETED:
        return None
    return row["response_json"] or ""


def complete(
    user_id: str,
    request_id: str,
    request_hash: str,
    lease_expires_at: str,
    response_json: str,
) -> bool:
    """只由当前租约所有者写入完成结果。"""
    conn = connect()
    try:
        updated = conn.execute(
            "UPDATE chat_request_dedup SET status='completed', response_json=?, "
            "lease_expires_at=NULL, updated_at=? WHERE user_id=? AND request_id=? "
            "AND request_hash=? AND status='processing' AND lease_expires_at=?",
            (
                response_json,
                _iso(_utc_now()),
                user_id,
                request_id,
                request_hash,
                lease_expires_at,
            ),
        )
        conn.commit()
        return updated.rowcount == 1
    except sqlite3.Error as exc:
        conn.rollback()
        if _is_missing_table(exc):
            raise RequestDedupUnavailable from exc
        raise
    finally:
        conn.close()


def release(user_id: str, request_id: str, request_hash: str, lease_expires_at: str) -> bool:
    """业务失败时释放当前占用，允许客户端安全重试。"""
    conn = connect()
    try:
        deleted = conn.execute(
            "DELETE FROM chat_request_dedup WHERE user_id=? AND request_id=? "
            "AND request_hash=? AND status='processing' AND lease_expires_at=?",
            (user_id, request_id, request_hash, lease_expires_at),
        )
        conn.commit()
        return deleted.rowcount == 1
    except sqlite3.Error as exc:
        conn.rollback()
        if _is_missing_table(exc):
            raise RequestDedupUnavailable from exc
        raise
    finally:
        conn.close()


def wait_for_completion(
    user_id: str,
    request_id: str,
    request_hash: str,
    *,
    timeout: float = DEFAULT_WAIT_SECONDS,
) -> str | None:
    """短暂等待其他进程完成；超时返回 None，由 API 返回可重试错误。"""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT request_hash, status, response_json FROM chat_request_dedup "
                "WHERE user_id=? AND request_id=?",
                (user_id, request_id),
            ).fetchone()
        except sqlite3.Error as exc:
            if _is_missing_table(exc):
                raise RequestDedupUnavailable from exc
            raise
        finally:
            conn.close()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            return None
        if row["status"] == COMPLETED:
            return row["response_json"] or ""
        if time.monotonic() >= deadline:
            return None
        time.sleep(WAIT_INTERVAL_SECONDS)


def cleanup_expired(
    *,
    completed_retention_seconds: float = COMPLETED_RETENTION_SECONDS,
) -> int:
    """清理过期处理租约和旧完成记录。"""
    now = _utc_now()
    processing_cutoff = _iso(now)
    completed_cutoff = _iso(now - timedelta(seconds=max(1.0, completed_retention_seconds)))
    conn = connect()
    try:
        cur = conn.execute(
            "DELETE FROM chat_request_dedup WHERE "
            "(status='processing' AND (lease_expires_at IS NULL OR lease_expires_at < ?)) "
            "OR (status='completed' AND updated_at < ?)",
            (processing_cutoff, completed_cutoff),
        )
        conn.commit()
        return cur.rowcount
    except sqlite3.Error as exc:
        conn.rollback()
        if _is_missing_table(exc):
            raise RequestDedupUnavailable from exc
        raise
    finally:
        conn.close()


def encode_response(response) -> str:
    """序列化 Pydantic 响应，兼容 Pydantic v1/v2。"""
    if hasattr(response, "model_dump"):
        payload = response.model_dump()
    elif hasattr(response, "dict"):
        payload = response.dict()
    else:
        payload = response
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def decode_response(response_json: str, response_type):
    """把持久化结果恢复成指定响应模型。"""
    payload = json.loads(response_json)
    if hasattr(response_type, "model_validate"):
        return response_type.model_validate(payload)
    return response_type.parse_obj(payload)
