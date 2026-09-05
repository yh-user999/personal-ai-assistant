"""FastAPI 应用实例：路由注册 + 生命周期钩子。

M1 里程碑：注册 chat 路由并跑通记忆检索注入。
M2 里程碑：注册 events/stats 路由。
M3 里程碑：注册 reports 路由 + Web 静态页 + 周报定时任务。
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import ClassVar

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.api import (
    chat,
    documents,
    events,
    executor,
    knowledge,
    novel,
    reminders,
    reports,
    stats,
)
from app.auth import authenticate_token
from app.config import settings
from app.core.scheduler import SchedulerManager
from app.models.database import init_db

# 统一日志：INFO 级别，含时间/级别/模块
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("assistant")

APP_VERSION = "0.4.1"  # 唯一版本来源：FastAPI 元数据与 /api/health 共用（v0.4 多人隔离）


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：建表 + 启动定时任务
    init_db()
    app.state.collector_heartbeat = None
    app.state.scheduler = SchedulerManager()
    await app.state.scheduler.start()
    yield
    # 关闭：停止定时任务
    await app.state.scheduler.stop()


class AuthMiddleware(BaseHTTPMiddleware):
    """API 鉴权：配置 token 后统一建立角色上下文并执行端点权限校验。

    白名单：静态页 "/"、健康检查 /api/health（无敏感数据）。
    每次请求实时读 settings.api_token（测试可 monkeypatch）。
    """

    PUBLIC_PATHS: ClassVar[set[str]] = {"/", "/api/health"}
    # 最长前缀优先：保证 "/api/knowledge/ingest" 这类更具体的规则
    # 先于父前缀 "/api/knowledge" 匹配（顺序 + break 的旧写法会让
    # 具体规则永不生效，宽严设置只能靠巧合保持正确）。
    ROLE_RULES: ClassVar[tuple] = tuple(sorted((
        ("/api/events", {"collector", "internal", "owner"}),
        ("/api/heartbeat", {"collector", "internal", "owner"}),
        ("/api/knowledge", {"owner", "internal"}),
        ("/api/documents", {"owner", "internal"}),
        ("/api/reports", {"owner", "internal"}),
        ("/api/daily", {"owner", "internal"}),
        ("/api/stats", {"owner", "internal"}),
        ("/api/reminders", {"owner", "internal"}),
        ("/api/messages", {"owner", "internal"}),
        ("/api/novel", {"owner", "internal"}),
        ("/api/mood", {"owner", "internal"}),
        ("/api/executor/pending", {"executor", "internal", "owner"}),
        ("/api/executor/results", {"executor", "internal", "owner"}),
        ("/api/executor/result", {"executor", "internal", "owner"}),
        ("/api/executor/enqueue", {"owner", "internal"}),
        ("/api/knowledge/ingest", {"owner", "internal"}),
        ("/api/documents/generate", {"owner", "internal"}),
        ("/api/reports/generate", {"owner", "internal"}),
    ), key=lambda rule: len(rule[0]), reverse=True))

    async def dispatch(self, request, call_next):
        path = request.url.path
        # 归一化尾斜杠：挂载在 "/" 的 StaticFiles 会抢先 FULL 匹配
        # /api/health/ 这类尾斜杠路径并返回 404，router 的 redirect_slashes
        # 根本没机会生效，所以在中间件里改写 scope 提前归一。
        if len(path) > 1 and path.endswith("/"):
            normalized = path.rstrip("/") or "/"
            if normalized.startswith("/api"):
                # 原位改写 scope：BaseHTTPMiddleware 的 call_next 闭包持有
                # 原始 request，新建 Request 不会传播到下游。
                request.scope["path"] = normalized
                request.scope["raw_path"] = normalized.encode()
                path = normalized
        # 静态资源（非 /api 路径）不走 API 鉴权：页面需要能自举加载，
        # 凭证由页面内 Token 栏填入后才用于 API 调用。
        if not path.startswith("/api/"):
            return await call_next(request)
        # 归一化尾斜杠：/api/health/ 与 /api/health 视为同一公开端点
        if path.rstrip("/") in self.PUBLIC_PATHS:
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        # 无任何 token 配置时保持旧的开放策略；一旦启用分类 token，统一建立上下文。
        configured = any(getattr(settings, name, "") for name in (
            "api_token", "owner_api_token", "internal_api_token",
            "collector_api_token", "executor_api_token", "qq_api_token",
        ))
        if configured:
            ctx = authenticate_token(token)
            if ctx is None:
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
            request.state.auth = ctx
            for prefix, allowed in self.ROLE_RULES:
                if path == prefix or path.startswith(prefix + "/"):
                    if ctx.role not in allowed:
                        return JSONResponse({"detail": "forbidden"}, status_code=403)
                    break
        return await call_next(request)


app = FastAPI(
    title="Personal AI Assistant",
    version=APP_VERSION,
    lifespan=lifespan,
)
app.add_middleware(AuthMiddleware)

# ── 路由 ──────────────────────────────────────────────────
app.include_router(chat.router, prefix="/api", tags=["chat"])
app.include_router(events.router, prefix="/api", tags=["events"])
app.include_router(stats.router, prefix="/api", tags=["stats"])
app.include_router(reports.router, prefix="/api", tags=["reports"])
app.include_router(knowledge.router, prefix="/api", tags=["knowledge"])
app.include_router(documents.router, prefix="/api", tags=["documents"])
app.include_router(executor.router, prefix="/api", tags=["executor"])
app.include_router(reminders.router, prefix="/api", tags=["reminders"])
app.include_router(novel.router, prefix="/api", tags=["novel"])


@app.get("/api/health")
async def health(request: Request):
    """健康检查：服务状态 + 采集器心跳（检测采集停滞）。"""
    hb = request.app.state.collector_heartbeat
    return {"status": "ok", "version": APP_VERSION, "collector_heartbeat": hb}


# ── Web 静态页 ────────────────────────────────────────────
# 注意：mount("/") 兜底所有路径，必须放在所有 API 路由之后注册，
# 否则会吞掉其后定义的 API（Starlette 按注册顺序匹配）。
_web_dir = Path(__file__).resolve().parent / "web" / "static"
if _web_dir.exists():
    app.mount("/", StaticFiles(directory=str(_web_dir), html=True), name="web")
