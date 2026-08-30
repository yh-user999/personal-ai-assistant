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
    deployment_env: str = "production"  # test/development 可显式关闭强制鉴权

    # ── LLM（OpenAI 兼容）───────────────────────────────────
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout: float = 60.0      # 单次调用超时秒数（SDK 默认 600s 太长，会挂死请求）
    llm_max_retries: int = 2       # 网络错误自动重试次数

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
    history_limit: int = 10  # 携带最近 N 轮原文（6.22：窗口加宽更懂上下文）；更早的用摘要续接

    # ── 执行器白名单（第 11 课）────────────────────────────
    # list_dir/read_file 允许的根目录（逗号分隔）；留空=禁止这两类操作
    # 字段名必须与环境变量名一致（pydantic-settings 大小写不敏感映射）
    executor_allowed_roots: str = ""

    # ── 脱敏（第 6.14 课：入库前统一脱敏）──────────────────
    # 自定义敏感词（分号/逗号分隔），入库文本命中即替换为"已脱敏"；
    # 手机号/邮箱/公网IP/身份证号由 sanitize 模块自动识别，无需配置
    sensitive_terms: str = ""

    # ── 定时任务 ────────────────────────────────────────────
    consolidation_interval_hours: float = 4.0
    weekly_report_weekday: int = 6       # 0=周一 … 6=周日
    weekly_report_hour: int = 21

    # ── API 鉴权（共享密钥，全部端点统一）──────────────────
    # 留空 = 不鉴权（仅限 Tailscale 内网等已隔离环境）
    api_token: str = ""

    # ── 采集器心跳 ──────────────────────────────────────────
    # 超过该秒数没心跳视为电脑不在线（执行器分支给 QQ 的提示文案用）
    heartbeat_stale_seconds: int = 120

    # ── QQ 推送（第 8 课：提醒唯一通道，仅 JD .env 配置）────
    # NapCat onebot HTTP 服务地址 + token + 主人 QQ（勿进仓库）
    qq_push_url: str = ""
    qq_push_token: str = ""
    qq_admin_id: str = ""

    @property
    def db_file(self) -> Path:
        p = Path(self.db_path)
        if not p.is_absolute():
            p = Path(__file__).resolve().parents[1] / p
        return p


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    # 配置校验 fail-fast：qq_admin_id 配错类型（非数字）曾导致推送每分钟
    # 抛 ValueError 被吞、全灭且无告警——启动期直接报清楚
    if s.qq_push_url and not s.qq_admin_id.strip().isdigit():
        raise ValueError(
            f"QQ_PUSH_URL 已配置但 QQ_ADMIN_ID='{s.qq_admin_id}' 不是数字，"
            "推送通道不可用——请修正 .env 或清空 QQ_PUSH_URL"
        )
    if s.deployment_env.casefold() == "production":
        if not s.api_token or len(s.api_token) < 32 or s.api_token == "change-me-random-string":
            raise ValueError(
                "生产环境必须配置至少 32 字符的随机 API_TOKEN，不能使用空值或模板占位符"
            )
    return s


settings = get_settings()
