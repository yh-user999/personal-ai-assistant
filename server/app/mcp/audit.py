"""MCP 调用审计：失败只告警，不阻断业务返回。"""
from __future__ import annotations

import inspect
import json
import logging
import sqlite3
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from functools import wraps
from typing import Any, TypeVar, cast

from app.models.database import connect

from .context import from_context
from .schemas import summarize_arguments

logger = logging.getLogger("assistant.mcp.audit")
_F = TypeVar("_F", bound=Callable[..., Any])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_tool_call(
    uid: str,
    role: str,
    tool: str,
    arguments_summary: Mapping[str, Any] | None,
    success: bool,
    error: str = "",
    duration_ms: int = 0,
    *,
    request_id: str = "",
    client_name: str = "",
) -> None:
    """写入一次 MCP 调用记录；不保存敏感参数全文。"""
    try:
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO mcp_audit_logs "
                "(user_id, role, tool, request_id, client_name, arguments_summary, "
                "success, error, duration_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uid,
                    role,
                    tool,
                    request_id[:160],
                    client_name[:160],
                    json.dumps(arguments_summary or {}, ensure_ascii=False, separators=(",", ":")),
                    1 if success else 0,
                    (error or "")[:500],
                    max(0, int(duration_ms)),
                    _now(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.Error, TypeError, ValueError) as exc:  # pragma: no cover - 审计故障不得影响主流程
        logger.warning("MCP 审计写入失败: %s", exc)


def _audit_arguments(fn: Callable[..., Any], args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """绑定调用参数后去掉 SDK Context，确保位置/关键字参数都被审计。"""
    try:
        bound = inspect.signature(fn).bind_partial(*args, **dict(kwargs))
        bound.apply_defaults()
        return {name: value for name, value in bound.arguments.items() if name != "ctx"}
    except (TypeError, ValueError):
        return {key: value for key, value in kwargs.items() if key != "ctx"}


def _context_argument(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> Any | None:
    value = kwargs.get("ctx")
    if value is not None:
        return value
    for candidate in reversed(args):
        if hasattr(candidate, "request_context") or hasattr(candidate, "uid"):
            return candidate
    return None


def audited_tool(fn: _F) -> _F:
    """为同步/异步工具统一记录成功、失败和耗时。"""
    if inspect.iscoroutinefunction(fn):

        @wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            ctx = from_context(_context_argument(args, kwargs))
            public_kwargs = _audit_arguments(fn, args, kwargs)
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:
                record_tool_call(
                    ctx.uid,
                    ctx.role,
                    fn.__name__,
                    summarize_arguments(public_kwargs),
                    False,
                    type(exc).__name__ + (f": {exc}" if str(exc) else ""),
                    round((time.perf_counter() - started) * 1000),
                    request_id=ctx.request_id,
                    client_name=ctx.client_name,
                )
                raise
            record_tool_call(
                ctx.uid,
                ctx.role,
                fn.__name__,
                summarize_arguments(public_kwargs),
                True,
                duration_ms=round((time.perf_counter() - started) * 1000),
                request_id=ctx.request_id,
                client_name=ctx.client_name,
            )
            return result

        return cast(_F, async_wrapper)

    @wraps(fn)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        ctx = from_context(_context_argument(args, kwargs))
        public_kwargs = _audit_arguments(fn, args, kwargs)
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            record_tool_call(
                ctx.uid, ctx.role, fn.__name__, summarize_arguments(public_kwargs), False,
                type(exc).__name__ + (f": {exc}" if str(exc) else ""),
                round((time.perf_counter() - started) * 1000),
                request_id=ctx.request_id, client_name=ctx.client_name,
            )
            raise
        record_tool_call(
            ctx.uid, ctx.role, fn.__name__, summarize_arguments(public_kwargs), True,
            duration_ms=round((time.perf_counter() - started) * 1000),
            request_id=ctx.request_id, client_name=ctx.client_name,
        )
        return result

    return cast(_F, sync_wrapper)
