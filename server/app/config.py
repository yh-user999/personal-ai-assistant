"""配置加载：读取环境变量 / .env，集中管理所有可调参数。"""
import re
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

MAX_LLM_API_KEYS = 8
MIN_LLM_API_KEY_LENGTH = 8


def parse_llm_api_keys(raw: str | None, legacy: str | None = None) -> list[str]:
    """解析多 Key 配置，并保留空项/重复项的可诊断错误。"""
    raw = raw or ""
    legacy = (legacy or "").strip()
    if not raw.strip():
        return [legacy] if legacy else []

    # 逗号后允许换行，单独的换行也可分隔；连续逗号和首尾分隔符仍视为空项。
    parts = re.split(r",\s*|\r?\n", raw)
    keys: list[str] = []
    seen: dict[str, int] = {}
    for index, part in enumerate(parts, 1):
        key = part.strip()
        if not key:
            raise ValueError(f"LLM_API_KEYS 第 {index} 项为空，请删除多余分隔符或填入 Key")
        previous = seen.get(key)
        if previous is not None:
            raise ValueError(f"LLM_API_KEYS 第 {index} 项与第 {previous} 项重复")
        seen[key] = index
        keys.append(key)

    if len(keys) > MAX_LLM_API_KEYS:
        raise ValueError(f"LLM_API_KEYS 最多支持 {MAX_LLM_API_KEYS} 个 Key，当前为 {len(keys)} 个")
    return keys


def mask_api_key(key: str, *, index: int | None = None) -> str:
    """生成不含原文的 Key 脱敏指纹，供日志和诊断输出使用。"""
    import hashlib

    fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    prefix = f"key[{index}]" if index is not None else "key"
    return f"{prefix}#{fingerprint}"


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
    # 新配置支持逗号/换行分隔多个 Key；为空时兼容旧的 LLM_API_KEY。
    llm_api_keys: str = ""
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    # 图片识别专用模型；普通聊天仍使用 LLM_MODEL。
    vision_llm_model: str = "deepseek-v4-flash-vision-exp"
    vision_max_image_bytes: int = 10 * 1024 * 1024
    vision_timeout: float = 90.0
    # 小说续写、章节分析、摘要等写作链路使用的模型。
    novel_llm_model: str = "omen-alpha"
    llm_timeout: float = 60.0      # 单次调用超时秒数（SDK 默认 600s 太长，会挂死请求）
    llm_max_retries: int = 2       # 应用层总重试预算（额外次数，SDK 内层重试关闭）
    llm_retry_backoff_seconds: float = 0.5  # 临时错误切换/重试前的基础退避
    llm_max_concurrency: int = 8  # 全局 LLM 请求并发上限
    llm_key_cooldown_seconds: float = 30.0  # Key 临时失败后的冷却时间

    # ── 回复审校（按需触发：命中风险/事实/情绪/道德信号才多调一次）──
    reflection_enabled: bool = True
    reflection_review_model: str = ""
    reflection_review_timeout: float = 10.0
    # 审校整体时间预算（秒）。审校是"锦上添花"的检查，不该拖慢聊天——
    # 实测一次审校曾占 31 秒；超预算就放弃审校、保留候选回复。
    reflection_review_budget: float = 14.0
    # 审校要输出 9 个分数 + 5 个布尔判定 + issues/revision_plan。
    # 700 太小：实测撞上限被截断，JSON 不完整 → 解析失败 → 审校永远"无效"。
    # 2000 给思考型模型留出余量（截断比多花 token 更糟）。
    reflection_max_tokens: int = 2000
    reflection_min_quality_score: float = 0.78
    reflection_long_reply_chars: int = 500
    reflection_max_revisions_per_turn: int = 1

    # ── LLM 自主响应规划（普通非流式聊天）───────────────
    # shadow_only=True 时只记录计划不改行为；默认全量生效。
    semantic_planner_enabled: bool = True
    semantic_planner_shadow_only: bool = False
    response_plan_model: str = ""
    response_plan_timeout: float = 10.0
    # 400 实测会被截断（plan JSON + 模型思考前缀），导致计划解析失败并静默降级
    response_plan_max_tokens: int = 1200
    response_plan_min_confidence: float = 0.60
    response_plan_max_history: int = 4

    # ── 实时信息检索（SearXNG 自托管；留空=整体关闭）─────────
    # 未配置时检索能力关闭，助手对实时问题只能回答"无法核实"，
    # 不会退回模型记忆作答。
    search_backend_url: str = ""
    search_timeout: float = 15.0
    search_max_results: int = 10
    search_max_page_bytes: int = 500_000
    search_time_range: str = "week"
    # 低于此结果数视为证据不足，触发阶梯式放宽（见 web_provider.search_and_cluster）
    search_min_results: int = 3
    web_search_enabled: bool = True
    # 声明级比对：仅在报道数 ≥ 此值时才做 LLM 抽取（单一信源无可比对）
    claim_analysis_enabled: bool = True
    claim_analysis_min_reports: int = 2
    # 声明表可能较长（多条 JSON），预算给足以免被截断——截断会导致 JSON
    # 不完整、解析失败而静默降级（planner/审校都踩过同一个坑）
    claim_analysis_max_tokens: int = 2400
    # 轻量模型对结构化抽取返回不稳定，在预算内重试；总预算适当放宽
    claim_analysis_retries: int = 2
    claim_analysis_budget: float = 20.0
    # 声明抽取专用模型：留空则回退到主模型。独立于 reflection_review_model
    # （那个被审校/planner 共用），改这个只影响声明抽取。
    claim_analysis_model: str = ""
    # GDELT 全球新闻事件库（第二检索源，走代理，与国内直连隔离）
    # 默认关闭：实测本机代理到 GDELT 的往返需 8 秒以上，与"不拖慢聊天"冲突——
    # 给足超时能命中但拖到 40~60 秒，压到 4 秒则连接建不起来。等有更快/专用
    # 出口时把此项改 true 即可启用，代码与去重/核查链路已就绪。
    gdelt_enabled: bool = False
    gdelt_proxy: str = "http://127.0.0.1:7890"   # 留空则不走代理
    gdelt_timeout: float = 8.0
    gdelt_max_results: int = 10
    # 国内热榜（今日头条+百度热搜，直连，答"最近有什么热点"这类浏览型问题）
    hotboard_enabled: bool = True
    hotboard_timeout: float = 6.0
    hotboard_max_items: int = 15

    # ── 价值基线与抗噪音（默认关闭，先观察再启用）────────────
    values_enabled: bool = True
    values_strict_stance: bool = True
    values_block_sensitive_details: bool = True
    values_noise_gate: bool = True

    # ── Embedding ───────────────────────────────────────────
    embedding_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    embedding_api_key: str = ""
    embedding_model: str = "embedding-3"
    embedding_dimension: int = 2048

    # ── 存储 ────────────────────────────────────────────────
    db_path: str = "./data/assistant.db"
    novel_root: str = "./data/novels"

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
    # 按角色分类的 token；为空时回退共享 API_TOKEN 策略
    owner_api_token: str = ""
    internal_api_token: str = ""
    collector_api_token: str = ""
    executor_api_token: str = ""
    qq_api_token: str = ""
    # QQ token 只证明请求来自插件；用户身份必须由该共享密钥签名。
    qq_identity_secret: str = ""
    qq_identity_max_age_seconds: int = 300

    # ── 采集器心跳 ──────────────────────────────────────────
    # 超过该秒数没心跳视为电脑不在线（执行器分支给 QQ 的提示文案用）
    heartbeat_stale_seconds: int = 120

    # ── QQ 推送（第 8 课：提醒唯一通道，仅 JD .env 配置）────
    # NapCat onebot HTTP 服务地址 + token + 主人 QQ（勿进仓库）
    qq_push_url: str = ""
    qq_push_token: str = ""
    qq_admin_id: str = ""

    # ── 主动开口（她"先找你"）────────────────────────────────
    # 默认关闭：这是行为上最大的改变（从"你问她答"变成"她会找你"），
    # 确认体验后再开。开启后仍有硬约束：每日 1 条上限、22:00-08:00 静默、
    # 连续 3 次无回应自动降频到每周 1 条。
    initiative_enabled: bool = False
    # 主动开口的检查时刻（本地时间）：22:10 = 每日小结（22:00）刚生成完
    initiative_hour: int = 22
    initiative_minute: int = 10
    # 静默时段 [start, 24) ∪ [0, end)：默认 23:00-08:00。
    # 注意不是 22:00——小结 22:00 才生成，22 点整段静默会让"今日一句"永远发不出
    # 去（只剩搁置话题）。想要 22 点后彻底安静就把 INITIATIVE_QUIET_START 设成 22
    # 并把 INITIATIVE_HOUR 调到 21。
    initiative_quiet_start: int = 23
    initiative_quiet_end: int = 8

    # ── 检索自愈（新体系类名自动兜底/登记，见 docs/检索自愈与答案自检方案.md）
    # 默认开：只在"判不出域/核心词未命中"的枚举式提问上触发，常规问题零开销。
    # 出问题可设 false 一键关闭（改动全是加性旁路，关掉即完全回到旧行为）。
    healer_enabled: bool = True

    # ── 请求决策轨迹（检索可观测性 P0）：每轮对话记一行决策链，可回放可统计。
    # 默认开（一次 INSERT/轮，零 LLM）；设 false 关闭。
    request_trace_enabled: bool = True

    # ── 行为上下文注入（聊天里的"用户当前状态"）。
    # 2026-09 评估后默认关：行为采集已默认关闭，这段注入只剩"暂无行为数据"
    # 占 prompt；想重开需同时开采集器通道与这个开关。
    behavior_inject_enabled: bool = False

    # ── 私人 MCP（默认关闭；独立 stdio 进程启动）────────────────
    # MCP Server 不随 FastAPI/uvicorn 启动，避免 stdout 与普通日志混用。
    mcp_enabled: bool = False
    mcp_stdio_role: str = "owner"       # owner / internal；stdio 默认只服务主人
    mcp_stdio_user_id: str = ""         # 留空时复用 owner_user_id()；仅供本地启动配置
    mcp_max_result_chars: int = 12000
    mcp_max_input_chars: int = 2000

    @property
    def llm_api_key_values(self) -> list[str]:
        """返回规范化后的 Key 列表；新配置为空时回退旧单 Key。"""
        return parse_llm_api_keys(self.llm_api_keys, self.llm_api_key)

    @property
    def db_file(self) -> Path:
        p = Path(self.db_path)
        if not p.is_absolute():
            p = Path(__file__).resolve().parents[1] / p
        return p


def _validate_llm_config(s: Settings) -> None:
    """校验 Key 池配置；错误信息只包含序号和长度，不回显密钥。"""
    keys = s.llm_api_key_values
    if s.llm_key_cooldown_seconds < 0:
        raise ValueError("LLM_KEY_COOLDOWN_SECONDS 不能小于 0")
    if s.llm_retry_backoff_seconds < 0:
        raise ValueError("LLM_RETRY_BACKOFF_SECONDS 不能小于 0")
    if s.llm_max_concurrency <= 0:
        raise ValueError("LLM_MAX_CONCURRENCY 必须大于 0")
    if s.vision_max_image_bytes <= 0:
        raise ValueError("VISION_MAX_IMAGE_BYTES 必须大于 0")
    if s.vision_timeout <= 0:
        raise ValueError("VISION_TIMEOUT 必须大于 0")
    if not str(s.vision_llm_model or "").strip():
        raise ValueError("VISION_LLM_MODEL 不能为空")
    if s.deployment_env.casefold() in {"production", "prod"}:
        short = [
            f"第 {index} 项（{len(key)} 字符）"
            for index, key in enumerate(keys, 1)
            if len(key) < MIN_LLM_API_KEY_LENGTH
        ]
        if short:
            raise ValueError(
                "生产环境 LLM Key 长度不足（最少 "
                f"{MIN_LLM_API_KEY_LENGTH} 字符）：" + ", ".join(short)
            )


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    _validate_llm_config(s)
    # 配置校验 fail-fast：qq_admin_id 配错类型（非数字）曾导致推送每分钟
    # 抛 ValueError 被吞、全灭且无告警——启动期直接报清楚
    if s.qq_push_url and not s.qq_admin_id.strip().isdigit():
        raise ValueError(
            f"QQ_PUSH_URL 已配置但 QQ_ADMIN_ID='{s.qq_admin_id}' 不是数字，"
            "推送通道不可用——请修正 .env 或清空 QQ_PUSH_URL"
        )
    if s.deployment_env.casefold() == "production":
        tokens = [s.api_token, s.owner_api_token, s.internal_api_token,
                  s.collector_api_token, s.executor_api_token, s.qq_api_token]
        if not any(t and len(t) >= 32 and t != "change-me-random-string" for t in tokens):
            raise ValueError(
                "生产环境必须配置至少一个 32 字符的随机 API token，不能使用空值或模板占位符"
            )
        if s.qq_api_token and not s.qq_identity_secret.strip():
            raise ValueError(
                "生产环境启用 QQ_API_TOKEN 时必须同时配置 QQ_IDENTITY_SECRET"
            )
    if s.qq_identity_max_age_seconds <= 0:
        raise ValueError("QQ_IDENTITY_MAX_AGE_SECONDS 必须大于 0")
    return s


settings = get_settings()
