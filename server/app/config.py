"""配置加载：读取环境变量 / .env，集中管理所有可调参数。"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # ── 定时任务 ────────────────────────────────────────────
    consolidation_interval_hours: float = 4.0
    weekly_report_weekday: int = 6       # 0=周一 … 6=周日
    weekly_report_hour: int = 21

    # ── 采集器推送鉴权（简单共享密钥，可选）────────────────
    collector_token: str = ""

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
