"""采集器配置：与根目录 .env 共用。"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


_REPO_ROOT = Path(__file__).resolve().parents[1]  # collector/ 的上一级 = 仓库根


class CollectorSettings(BaseSettings):
    model_config = SettingsConfigDict(
        # parents[1] 才是仓库根（collector/config.py → 仓库根）。
        # 原先写 parents[2] 会指到仓库外层目录，.env 从未被 pydantic 读到——
        # 只因 main.py 先跑 load_dotenv 注入环境变量才碰巧生效；
        # 单独导入本模块（测试/脚本）时全部配置静默回落默认值。
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 服务器地址（Tailscale 内网地址优先）
    server_url: str = "http://127.0.0.1:8000"
    # API 鉴权令牌（与服务器 .env 的 API_TOKEN 一致）
    api_token: str = ""

    # 采集开关（2026-09 评估后默认关：行为事件价值密度低，只留心跳+执行器；
    # 想重新打开在 .env 里设 COLLECT_WINDOW/COLLECT_BROWSER/COLLECT_GIT=true）
    collect_window: bool = False
    collect_browser: bool = False
    collect_git: bool = False

    # 间隔（秒）
    window_interval: float = 8.0
    browser_interval: float = 600.0   # 10 分钟
    git_interval: float = 900.0       # 15 分钟

    # git 扫描的项目目录（逗号分隔）
    git_repos: str = ""

    # 隐私过滤：事件离开本机前脱敏（密码/token/手机号/邮箱等）
    privacy_filter: bool = True

    # 缓存位置（断网落盘队列 + 浏览器/git 增量游标）
    cache_dir: str = "./cache"
    cache_retry_interval: float = 60.0  # 运行期间重放 pending 文件的间隔秒数

    @property
    def cache_path(self) -> Path:
        """缓存目录的绝对路径。

        相对路径以 collector/ 为基准而非进程 CWD——开机自启时 CWD 常是
        C:\\Windows\\System32，按 CWD 解析会把队列与游标写到意外位置
        （甚至因无权限写入而静默丢事件）。
        """
        p = Path(self.cache_dir)
        if not p.is_absolute():
            p = Path(__file__).resolve().parent / p
        return p


@lru_cache
def get_collector_settings() -> CollectorSettings:
    return CollectorSettings()


settings = get_collector_settings()
