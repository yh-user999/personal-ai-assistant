"""FastAPI 应用实例：路由注册 + 生命周期钩子。

M1 里程碑：注册 chat 路由并跑通记忆检索注入。
M2 里程碑：注册 events/stats 路由。
M3 里程碑：注册 reports 路由 + Web 静态页 + 周报定时任务。
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from app.api import chat, events, reports, stats
from app.core.scheduler import SchedulerManager
from app.models.database import init_db

# 统一日志：INFO 级别，含时间/级别/模块
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("assistant")


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


@app.get("/api/health")
async def health(request: Request):
    """健康检查：服务状态 + 采集器心跳（检测采集停滞）。"""
    hb = request.app.state.collector_heartbeat
    return {"status": "ok", "version": "0.2.0", "collector_heartbeat": hb}


# ── Web 静态页 ────────────────────────────────────────────
# 注意：mount("/") 兜底所有路径，必须放在所有 API 路由之后注册，
# 否则会吞掉其后定义的 API（Starlette 按注册顺序匹配）。
_web_dir = Path(__file__).resolve().parent / "web" / "static"
if _web_dir.exists():
    app.mount("/", StaticFiles(directory=str(_web_dir), html=True), name="web")
