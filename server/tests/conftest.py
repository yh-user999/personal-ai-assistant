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
# 本地 .env 的真实 QQ_ADMIN_ID 会让 owner_user_id() 返回真实 QQ 号而不是 'owner'
# 哨兵，多人隔离/淘汰/画像等 12 个用例随之失败。测试固定用 'owner' 语义，
# 需要真实 admin id 的用例自行 monkeypatch settings.qq_admin_id。
# 用 setdefault 而非强制覆盖：CI 里显式导出该变量时仍可生效。
os.environ.setdefault("QQ_ADMIN_ID", "")
os.environ.setdefault("QQ_PUSH_URL", "")



# ── 数据库测试共享设施 ────────────────────────────────────
# 长驻线程本地连接（database.connect 缓存）要求切换 db_path 后 reset；
# 各测试文件用共享的 db fixture（monkeypatch db_path → init_db → reset）。

import pytest

from app.config import Settings, settings
from app.models.database import init_db, reset_connections


@pytest.fixture
def db(tmp_path, monkeypatch):
    """独立临时库 + 连接缓存重置。16 份重复 fixture 收编于此。"""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    reset_connections()
    init_db()
    yield
    reset_connections()


# ── 生产库护栏（血的教训）────────────────────────────────────
# 曾有 14 个测试文件靠 os.environ.setdefault("DB_PATH", "/tmp/...") 做隔离，
# 但本文件在收集阶段就 import 了 app.config，而 get_settings() 带 @lru_cache——
# 环境变量设置为时已晚，settings.db_path 始终是 ./data/assistant.db。
# 那些 fixture 里的 DELETE FROM memories / knowledge_chunks 于是直接跑在真实库上，
# 一次全量测试清掉了 640 条对话记忆与 3600 个知识库块。
# 隔离靠自觉不可靠，这里改成机器强制：每个用例执行前后都校验库路径。

# 生产库绝对路径：直接问 Settings 自己（db_file 里有相对路径的解析规则），
# 不在这里复制一份解析逻辑——复制就会漂移。
PRODUCTION_DB = Settings(db_path="./data/assistant.db").db_file.resolve()


def _is_production_db(db_path: str) -> bool:
    """只认那一个真实库文件。

    不能用 endswith("data/assistant.db") 判断——test_backup 故意把库放在
    tmp_path/data/assistant.db（要验证备份目录是库的兄弟目录），会被误报。
    """
    try:
        return Settings(db_path=db_path).db_file.resolve() == PRODUCTION_DB
    except (OSError, ValueError):
        return False


def pytest_configure(config):
    """会话最开始就把 db_path 从生产库挪开（fail-safe 而非 fail-loud）。

    护栏分两层，这是第一层——兜底：即使某个测试文件完全忘了隔离，它拿到的
    也是会话级临时库，而不是 ./data/assistant.db。放在 pytest_configure 里
    是因为它早于任何 fixture 与用例执行。
    """
    import tempfile
    from pathlib import Path

    if _is_production_db(settings.db_path):
        fallback = Path(tempfile.mkdtemp(prefix="pytest-db-")) / "session.db"
        settings.db_path = str(fallback)
        reset_connections()
        config._prod_db_guarded = True


def pytest_sessionstart(session):
    """第二层——在 connect() 上装拦截器：测试期任何指向生产库的连接一律拒绝。

    为什么不用 fixture 的前后断言：那是 time-of-check，拦不住"用例执行途中
    把 db_path 改回生产库"这种情况（monkeypatch 是函数级的，会在 autouse
    fixture 的 teardown 之前就把值还原，后置断言永远看到干净值）。
    改成 time-of-use——真正打开连接的那一刻校验，无从绕过。
    """
    from app.models import database

    real_connect = database.connect

    def guarded_connect():
        if _is_production_db(settings.db_path):
            raise RuntimeError(
                f"测试试图连接生产库 {PRODUCTION_DB}。\n"
                "用 conftest 的 db fixture 或 monkeypatch.setattr(settings, 'db_path', ...)；\n"
                "os.environ.setdefault('DB_PATH', ...) 无效——settings 是 lru_cache 单例，"
                "conftest 收集阶段已经实例化过了。"
            )
        return real_connect()

    database.connect = guarded_connect
    session._real_connect = real_connect


def pytest_sessionfinish(session, exitstatus):
    """还原 connect（同一进程里后续若有别的用途，不留副作用）。"""
    real = getattr(session, "_real_connect", None)
    if real is not None:
        from app.models import database

        database.connect = real


@pytest.fixture(autouse=True)
def _reset_conn_cache():
    """任何测试结束后丢弃连接缓存，防止跨测试泄漏到下一个 db_path。"""
    yield
    reset_connections()
