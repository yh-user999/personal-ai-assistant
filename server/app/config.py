"""配置加载：读取环境变量 / .env，集中管理所有可调参数。"""
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── 服务 ────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000

    # ── LLM（OpenAI 兼容）───────────────────────────────────
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"

    # ── Embedding ───────────────────────────────────────────
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_api_key: str = ""
    embedding_model: str = "Qwen3-Embedding-0.6B"
    embedding_dimension: int = 1024

    # ── 存储 ────────────────────────────────────────────────
    db_path: str = "./data/assistant.db"

    # ── 记忆检索 ────────────────────────────────────────────
    inject_top_k: int = 5
    min_similarity: float = 0.35
    importance_decay_days: float = 30.0  # importance 半衰期

    # ── 多轮上下文 ──────────────────────────────────────────
    history_limit: int = 8  # 携带最近 N 轮原文；更早的用摘要续接

    # ── 执行器白名单（第 11 课）────────────────────────────
    # list_dir/read_file 允许的根目录（逗号分隔）；留空=禁止这两类操作
    # NoDecode：值含逗号时 pydantic-settings 会误当 JSON 解析（失败回退默认）
    executor_roots: Annotated[str, NoDecode] = ""

    # ── 定时任务 ────────────────────────────────────────────
    consolidation_interval_hours: float = 4.0
    weekly_report_weekday: int = 6       # 0=周一 … 6=周日
    weekly_report_hour: int = 21

    # ── API 鉴权（共享密钥，全部端点统一）──────────────────
    # 留空 = 不鉴权（仅限 Tailscale 内网等已隔离环境）
    api_token: str = ""

    @property
    def db_file(self) -> Path:
        p = Path(self.db_path)
        if not p.is_absolute():
            p = Path(__file__).resolve().parents[1] / p
        return p


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
