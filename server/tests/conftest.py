"""pytest 共享配置：统一 sys.path 与测试环境隔离。

- app.*（服务端代码）需要 server/ 在 path 上
- collector.* / desktop.* / common.*（跨端模块）需要仓库根在 path 上
- 环境变量在 app.config 首次导入前兜底：本地 .env 里的真实 API_TOKEN 会让
  未处理鉴权的老测试 401——测试环境默认关鉴权，鉴权用例自行 monkeypatch
"""
import os
import sys
from pathlib import Path

SERVER_ROOT = str(Path(__file__).resolve().parents[1])
REPO_ROOT = str(Path(__file__).resolve().parents[2])

for _p in (SERVER_ROOT, REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("API_TOKEN", "")
os.environ.setdefault("DEPLOYMENT_ENV", "test")



# ── 数据库测试共享设施 ────────────────────────────────────
# 长驻线程本地连接（database.connect 缓存）要求切换 db_path 后 reset；
# 各测试文件用共享的 db fixture（monkeypatch db_path → init_db → reset）。

import pytest  # noqa: E402

from app.config import settings  # noqa: E402
from app.models.database import init_db, reset_connections  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    """独立临时库 + 连接缓存重置。16 份重复 fixture 收编于此。"""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    reset_connections()
    init_db()
    yield
    reset_connections()


@pytest.fixture(autouse=True)
def _reset_conn_cache():
    """任何测试结束后丢弃连接缓存，防止跨测试泄漏到下一个 db_path。"""
    yield
    reset_connections()
