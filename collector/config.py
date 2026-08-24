"""采集器配置：与根目录 .env 共用。"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class CollectorSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 服务器地址（Tailscale 内网地址优先）
    server_url: str = "http://127.0.0.1:8000"
    collector_token: str = ""

    # 采集开关
    collect_window: bool = True
    collect_browser: bool = True
    collect_git: bool = True

    # 间隔（秒）
    window_interval: float = 8.0
    browser_interval: float = 600.0   # 10 分钟
    git_interval: float = 900.0       # 15 分钟

    # git 扫描的项目目录（逗号分隔）
    git_repos: str = ""

    # 隐私过滤：事件离开本机前脱敏（密码/token/手机号/邮箱等）
    privacy_filter: bool = True

    # 浏览器历史缓存位置（增量游标）
    cache_dir: str = "./cache"


@lru_cache
def get_collector_settings() -> CollectorSettings:
    return CollectorSettings()


settings = get_collector_settings()
