"""FastAPI 应用实例：路由注册 + 生命周期钩子。

M1 里程碑：注册 chat 路由并跑通记忆检索注入。
M2 里程碑：注册 events/stats 路由。
M3 里程碑：注册 reports 路由 + Web 静态页 + 周报定时任务。
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from app.api import chat, events, reports, stats
from app.core.scheduler import SchedulerManager
from app.models.database import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：建表 + 启动定时任务
    init_db()
    app.state.scheduler = SchedulerManager()
    await app.state.scheduler.start()
    yield
    # 关闭：停止定时任务
    await app.state.scheduler.stop()


app = FastAPI(
    title="Personal AI Assistant",
    version="0.1.0",
    lifespan=lifespan,
)

# ── 路由 ──────────────────────────────────────────────────
app.include_router(chat.router, prefix="/api", tags=["chat"])
app.include_router(events.router, prefix="/api", tags=["events"])
app.include_router(stats.router, prefix="/api", tags=["stats"])
app.include_router(reports.router, prefix="/api", tags=["reports"])

# ── Web 静态页 ────────────────────────────────────────────
_web_dir = Path(__file__).resolve().parent / "web" / "static"
if _web_dir.exists():
    app.mount("/", StaticFiles(directory=str(_web_dir), html=True), name="web")


@app.get("/api/health")
async def health():
    """健康检查：确认各依赖服务可用。"""
    return {"status": "ok", "version": "0.1.0"}
