"""SQLite 数据层：建表 + sqlite-vec 向量表。

表结构与 docs/实施方案细则.md 第四节对应。
注意：sqlite-vec 是扩展，需在连接时 load_extension。
"""
import logging
import sqlite3
from datetime import datetime, timezone

from app.config import settings

logger = logging.getLogger("assistant.db")

SCHEMA_VERSION = 9

_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  version INTEGER NOT NULL,
  applied_at TEXT NOT NULL
);

-- ① 对话记忆（情境记忆）
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT '',    -- 用户标识：主人 QQ 号 / 'owner'（未配置时）；访客为其 QQ 号
  sender TEXT NOT NULL,               -- 'user' / 'assistant'
  content TEXT NOT NULL,
  summary TEXT DEFAULT '',
  topics TEXT DEFAULT '[]',           -- JSON 数组
  ts TEXT NOT NULL,
  importance REAL DEFAULT 1.0
);

-- ② 事实表（永久知识，三元组，按用户隔离）
CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL,
  predicate TEXT NOT NULL,
  object TEXT NOT NULL,
  source_memory_id INTEGER,
  confidence REAL DEFAULT 0.7,
  updated_at TEXT NOT NULL,
  UNIQUE(user_id, subject, predicate, object)
);

-- ③ 画像表（四维度，按用户隔离）
CREATE TABLE IF NOT EXISTS profile (
  user_id TEXT NOT NULL DEFAULT '',
  dimension TEXT NOT NULL,            -- technical_background / work_habit / learning_rhythm / project_info
  value TEXT NOT NULL,
  confidence REAL DEFAULT 0.5,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(user_id, dimension)
);

-- ④ 工作日志（手动记录）
CREATE TABLE IF NOT EXISTS work_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT '',
  date TEXT NOT NULL,
  time_range TEXT DEFAULT '',
  content TEXT NOT NULL,
  project TEXT DEFAULT '',
  tags TEXT DEFAULT '[]',
  created_at TEXT NOT NULL
);

-- ⑤ 行为事件（采集器推送）
CREATE TABLE IF NOT EXISTS behavior_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT '',     -- 采集器身份范围；collector token 固定归属主人
  event_id TEXT,                      -- 采集器稳定幂等键；旧客户端可为空
  kind TEXT NOT NULL,                 -- app_usage / browser / git_commit / manual
  name TEXT NOT NULL,
  detail TEXT DEFAULT '',
  start_ts TEXT,
  end_ts TEXT,
  meta TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_behavior_kind ON behavior_events(kind);
CREATE INDEX IF NOT EXISTS idx_behavior_start ON behavior_events(start_ts);

-- ⑥ 周报归档
CREATE TABLE IF NOT EXISTS weekly_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT '',
  week TEXT NOT NULL,                 -- '2025-W25'
  content TEXT NOT NULL,
  stats TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(user_id, week)
);

-- 查询索引（v0.3 采纳评审建议：常用查询字段建索引）
-- 注意：idx_memories_user 依赖 user_id 列，老库迁移时在 _migrate_user_id 里
-- 加列后再建（放这里会让老库的 executescript 中途炸掉）
CREATE INDEX IF NOT EXISTS idx_memories_ts ON memories(ts);
CREATE INDEX IF NOT EXISTS idx_facts_updated ON facts(updated_at);
CREATE INDEX IF NOT EXISTS idx_worklog_created ON work_log(created_at);

-- ⑦ 教训表（自省模块：用户纠正的内容，高优先级长期记忆）
-- UNIQUE(content)：同一句纠正只留一行（重复纠正刷新时间而非再插一份）——
-- 没有这条约束时 52 行里只有 7 条不同内容，注入窗口被副本挤满，
-- "你就叫小月吧"这类身份设定反而进不了 prompt。
CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL,            -- 用户纠正的原话
  context TEXT DEFAULT '',          -- 被纠正的 AI 回复（上下文）
  created_at TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'style',  -- identity（身份设定，永久优先）/ style / fact
  hit_count INTEGER DEFAULT 0,
  last_hit_at TEXT,
  UNIQUE(user_id, content)
);

-- ⑧ 每日小结（每晚 22:00 生成当天工作摘要）
CREATE TABLE IF NOT EXISTS daily_summaries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT '',
  date TEXT NOT NULL,                 -- '2026-08-25'
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(user_id, date)
);

-- ⑨ 关切话题（用户最近在意的主题，mention_count 追踪活跃度，按用户隔离）
CREATE TABLE IF NOT EXISTS concerns (
  user_id TEXT NOT NULL DEFAULT '',
  topic TEXT NOT NULL,
  mention_count INTEGER DEFAULT 1,
  last_mentioned_at TEXT NOT NULL,
  asked_at TEXT,                      -- 主动问过"这事后来怎么样了"的时间（问过不再问第二次）
  PRIMARY KEY(user_id, topic)
);

-- ⑩ 术语词典（用户问过的技术名词 → 解释，保证口径一致，按用户隔离）
CREATE TABLE IF NOT EXISTS jargon_terms (
  user_id TEXT NOT NULL DEFAULT '',
  term TEXT NOT NULL,
  explanation TEXT NOT NULL,
  created_at TEXT NOT NULL,
  times_used INTEGER DEFAULT 0,
  PRIMARY KEY(user_id, term)
);

-- ⑪ 风格范例（用户满意的回复，few-shot 注入，按用户隔离）
CREATE TABLE IF NOT EXISTS style_examples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- ⑫ 知识库块（RAG：文档切块后的文本，与 chunk_vectors 一一对应）
-- domain 分域：所有文档混在一张表里检索会严重跨域污染。实测问
-- 「李羽的能力是什么」（《寂静杀戮》角色）命中 6 块**全部无关**——4 块来自
-- 另一本小说、2 块来自 LESSONS.md；问「命丛有哪些」命中了反代教程 PDF。
-- 根因是 embedding 各向异性：实测所有块的相似度都塌在 0.023~0.025 这个
-- 0.002 宽的区间里，向量对"相关/无关"没有区分力，只能靠元数据过滤。
CREATE TABLE IF NOT EXISTS knowledge_chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_name TEXT NOT NULL,           -- 来源文档名
  chunk_index INTEGER NOT NULL,     -- 在文档中的块序号
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  domain TEXT NOT NULL DEFAULT ''   -- novel / project_doc / manual / resume
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON knowledge_chunks(doc_name);
CREATE INDEX IF NOT EXISTS idx_chunks_domain ON knowledge_chunks(domain);

-- ⑬ 生成的文档（对话式"写文档"保存的产物，同时同步进知识库）
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- ⑭ 目标（Goal 系统：用户的目标与进度，周报核对，按用户隔离）
CREATE TABLE IF NOT EXISTS goals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL,
  status TEXT DEFAULT 'active',    -- active / done / paused
  progress TEXT DEFAULT '',        -- 进度备注
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- ⑮ 未解决问题（unresolved：聊到一半被打断的话题，按用户隔离）
CREATE TABLE IF NOT EXISTS unresolved_issues (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT '',
  topic TEXT NOT NULL,
  context TEXT DEFAULT '',
  status TEXT DEFAULT 'open',      -- open / resolved
  created_at TEXT NOT NULL,
  resolved_at TEXT
);

-- ⑯ 执行器指令队列（第 11 课：机器人操作 Windows 的通道）
CREATE TABLE IF NOT EXISTS executor_commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action TEXT NOT NULL,            -- open / list_dir / read_file / copy / backup / move / rename
  target TEXT NOT NULL,
  status TEXT DEFAULT 'pending',   -- pending / claimed / done / failed / unknown
  result TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  claimed_at TEXT,
  executed_at TEXT,
  device_id TEXT DEFAULT '',
  claim_token TEXT DEFAULT '',
  lease_expires_at TEXT,
  result_late INTEGER DEFAULT 0
);

-- ⑰ 小说设定卡（第 6.19 课：策划数据，人物/事件权威事实）
CREATE TABLE IF NOT EXISTS novel_facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book TEXT NOT NULL,              -- 所属书（如 小说-寂静杀戮）
  keywords TEXT NOT NULL,          -- 触发词（逗号分隔：人物名/事件词）
  content TEXT NOT NULL,           -- 设定条目正文
  created_at TEXT NOT NULL
);

-- ㉔ 小说实体表（专名索引，不是答案缓存）
-- 动因：问"有哪些命丛"时向量检索几乎零区分力（实测 top3 全是无关 PDF，
-- 小说排第四且相似度 0.023 vs 0.025 无差别），而 FTS5 搜类名「命丛」命中
-- 308/1936 块 = 15.9% 精度，等于没筛。但搜专名「银河灵潮」只命中 1 块。
-- 这张表的唯一作用：把低精度的类名匹配转成高精度的专名匹配。
-- 存的是**名字**（客观、唯一、不随提问变化），不存问答结果——
-- 缓存答案会随提问维度爆炸且互相矛盾。
CREATE TABLE IF NOT EXISTS novel_entities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book TEXT NOT NULL,              -- 所属书
  name TEXT NOT NULL,              -- 专名（夜海 / 银河灵潮）
  kind TEXT NOT NULL,              -- 命丛 / 命图 / 功法 / 势力 / 人物
  group_name TEXT DEFAULT '',      -- 原文提到的集合（七大神命丛/四种命图），用来算缺口
  first_chunk INTEGER,             -- 首次出现的块序号（溯源用）
  verified INTEGER DEFAULT 0,      -- 用户确认过（1）还是仅 LLM 抽取（0）
  note TEXT DEFAULT '',            -- 用户的修订，优先于原文
  created_at TEXT NOT NULL,
  UNIQUE(book, name, kind)
);
CREATE INDEX IF NOT EXISTS idx_entities_book_kind ON novel_entities(book, kind);

-- ⑱ 定时提醒（第 6.24 课：机器人主动触达的最小通道）
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL,           -- 提醒内容
  remind_at TEXT NOT NULL,         -- 提醒时间（ISO, UTC）
  status TEXT DEFAULT 'pending',   -- pending / sending / notified / cancelled
  created_at TEXT NOT NULL,
  sending_token TEXT DEFAULT '',
  sending_at TEXT,
  sending_lease_expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, remind_at);

-- ⑲ 写作台账（第 6.25 课：小说写作增强）
CREATE TABLE IF NOT EXISTS writing_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT '',
  chapter TEXT,                    -- 章节号（可空）
  words INTEGER NOT NULL,          -- 本次字数
  created_at TEXT NOT NULL
);

-- ⑳ 情绪日志（第 6.27 课：情绪记忆层 + 反馈闭环）
CREATE TABLE IF NOT EXISTS mood_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT '',
  mood TEXT NOT NULL,              -- 疲惫 / 着急 / 烦躁 / 开心 / 低落
  snippet TEXT DEFAULT '',         -- 触发消息摘要（≤80 字）
  created_at TEXT NOT NULL
);

-- ㉑ 健身台账（第 6.29 课：健身减脂助手）
CREATE TABLE IF NOT EXISTS fitness_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,              -- weight / training
  value REAL,                      -- 体重数值（训练记录为空）
  detail TEXT DEFAULT '',          -- 训练内容描述
  created_at TEXT NOT NULL
);

-- ㉓ 主动开口台账（她"先找你"的记录：每日上限、无回应降频都靠这张表）
-- responded：推送后用户是否回过话（0/1）；连续无回应即自动降频。
CREATE TABLE IF NOT EXISTS initiative_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,              -- daily（今日一句）/ concern（搁置话题续上）
  content TEXT NOT NULL,
  topic TEXT DEFAULT '',           -- concern 类的话题（同话题不问第二次）
  sent_at TEXT NOT NULL,
  responded INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_initiative_sent ON initiative_log(sent_at);

-- ㉒ 健身知识卡（第 6.29 课：权威指南提炼，仿小说设定卡）
CREATE TABLE IF NOT EXISTS fitness_facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book TEXT NOT NULL,              -- 固定 '健身'
  keywords TEXT NOT NULL,          -- 触发词（逗号分隔）
  content TEXT NOT NULL,           -- 权威条目正文（含出处年份）
  created_at TEXT NOT NULL
);

-- ㉔ 动态类名词表（检索自愈一期）：用户问过的、硬编码词表未覆盖的体系类名。
-- domain='novel' 的词会并入域路由的小说类名表；'' 表示仅登记不参与路由
-- （聚合块不足以判定领域归属时）。幂等靠 UNIQUE(class_word)。
CREATE TABLE IF NOT EXISTS dynamic_classes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  class_word TEXT NOT NULL UNIQUE,
  domain TEXT NOT NULL DEFAULT '',  -- 'novel' / ''
  source_query TEXT DEFAULT '',
  hit_count INTEGER DEFAULT 0,
  created_at TEXT NOT NULL,
  last_hit_at TEXT
);

-- ㉕ 请求决策轨迹（检索可观测性 P0）：每轮对话的检索决策链可回放。
-- routing=域路由结果 JSON；retrieval_path=实体索引/hybrid/heal/skip；
-- healer=自愈触发词 JSON；injection_bytes=各注入段字节数 JSON。
-- 写入是 fire-and-forget 后台任务，失败不影响回复；30 天由 evict_stale 清理。
CREATE TABLE IF NOT EXISTS request_traces (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT '',
  query TEXT NOT NULL,
  ts TEXT NOT NULL,
  routing TEXT DEFAULT '{}',
  retrieval_path TEXT DEFAULT '',
  vector_degraded INTEGER DEFAULT 0,
  healer TEXT DEFAULT '',
  injection_bytes TEXT DEFAULT '{}',
  search_ms INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_traces_ts ON request_traces(ts);

-- ㉖ 聊天请求幂等：跨线程/进程/重启保留 request_id 的最终状态。
CREATE TABLE IF NOT EXISTS chat_request_dedup (
  user_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'processing', -- processing / completed
  response_json TEXT NOT NULL DEFAULT '',
  lease_expires_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(user_id, request_id)
);
CREATE INDEX IF NOT EXISTS idx_chat_dedup_lease ON chat_request_dedup(status, lease_expires_at);

-- ㉗ LLM 持久化用量：只记录统计元数据，不保存密钥原文或提示词。
CREATE TABLE IF NOT EXISTS llm_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT '',
  request_id TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  key_index INTEGER NOT NULL DEFAULT 0,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  cached_tokens INTEGER NOT NULL DEFAULT 0,
  fallback_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON llm_usage(created_at);
CREATE INDEX IF NOT EXISTS idx_llm_usage_model ON llm_usage(model, created_at);

-- ㉘ MCP 工具审计：只记录调用摘要，不保存记忆/facts 全文或密钥。
CREATE TABLE IF NOT EXISTS mcp_audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL DEFAULT '',
  tool TEXT NOT NULL,
  request_id TEXT DEFAULT '',
  client_name TEXT DEFAULT '',
  arguments_summary TEXT DEFAULT '{}',
  success INTEGER NOT NULL DEFAULT 0,
  error TEXT DEFAULT '',
  duration_ms INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mcp_audit_created ON mcp_audit_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_mcp_audit_tool ON mcp_audit_logs(tool, created_at);

-- ㉗ 自动实体抽取台账（检索自愈二期）：预算闸与幂等闸的数据源。
CREATE TABLE IF NOT EXISTS auto_extract_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind_word TEXT NOT NULL,
  book TEXT DEFAULT '',
  extracted_at TEXT NOT NULL,
  names_count INTEGER DEFAULT 0,
  day_key TEXT DEFAULT ''   -- 'YYYY-MM-DD:词'：幂等闸的原子占位键
);
CREATE INDEX IF NOT EXISTS idx_autoextract_at ON auto_extract_log(extracted_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_autoextract_day_key ON auto_extract_log(day_key);

-- ㉗ 实体候选池（低置信抽取结果，人工确认后转正）。
CREATE TABLE IF NOT EXISTS entity_candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book TEXT NOT NULL,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  first_chunk INTEGER,
  status TEXT DEFAULT 'pending',   -- pending / confirmed / discarded
  created_at TEXT NOT NULL,
  UNIQUE(book, kind, name)
);

-- ㉘ 索引纠错台账（检索自愈三期：用户纠错的审计留痕）。

-- ㉙ 黑话表（黑话模块一至三期）：词/短句在圈内的语境语义。
-- scope：shared=主人维护、访客只读；private=各人私有。
-- status：candidate=语境推断的候选（用一次信一分，≥2 次转正）/ confirmed。
CREATE TABLE IF NOT EXISTS slang_terms (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT '',
  term TEXT NOT NULL,
  meaning TEXT NOT NULL,
  context_hint TEXT DEFAULT '',
  source_episode TEXT DEFAULT '',
  scope TEXT NOT NULL DEFAULT 'private',
  status TEXT NOT NULL DEFAULT 'confirmed',
  use_count INTEGER DEFAULT 0,
  last_used_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(user_id, term, context_hint)
);
CREATE INDEX IF NOT EXISTS idx_slang_term ON slang_terms(term);
CREATE TABLE IF NOT EXISTS index_corrections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target TEXT NOT NULL,
  reason TEXT DEFAULT '',
  corrected_at TEXT NOT NULL
);

-- ㉚ 章节剧情存档（小说写作增强二期）：每章一句话摘要 + 未回收伏笔。
-- 跨章连贯的权威依据：写第 N 章前把前情注入 prompt，分析时自动更新。
-- source: manual（用户"章节存档"命令）/ analysis（章节分析落档）/ auto（生成后被动抓取）。
-- 幂等靠 UNIQUE(chapter)——同章重复分析只更新，不堆重复行。
CREATE TABLE IF NOT EXISTS chapter_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chapter TEXT NOT NULL UNIQUE,    -- '1' / '12'（统一阿拉伯数字字符串）
  summary TEXT NOT NULL,           -- 一句话剧情摘要
  threads TEXT DEFAULT '[]',       -- JSON 数组：本章埋下/应回收的伏笔
  source TEXT DEFAULT 'manual',    -- manual / analysis / auto
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- ㉛ 小说项目、章节正文/草稿与可恢复生成任务（二期）
CREATE TABLE IF NOT EXISTS novel_projects (
  project_id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  name TEXT NOT NULL,
  slug TEXT NOT NULL UNIQUE,
  root TEXT,
  metadata TEXT NOT NULL DEFAULT '{}',
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_novel_projects_owner ON novel_projects(owner_id, updated_at);

CREATE TABLE IF NOT EXISTS novel_project_members (
  project_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'member',
  created_at TEXT NOT NULL,
  PRIMARY KEY(project_id, user_id),
  FOREIGN KEY(project_id) REFERENCES novel_projects(project_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_novel_members_user ON novel_project_members(user_id, project_id);

CREATE TABLE IF NOT EXISTS novel_chapters (
  project_id TEXT NOT NULL,
  chapter_no TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL DEFAULT '',
  draft_content TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(project_id, chapter_no),
  FOREIGN KEY(project_id) REFERENCES novel_projects(project_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_novel_chapters_project ON novel_chapters(project_id, chapter_no);

CREATE TABLE IF NOT EXISTS novel_generation_jobs (
  job_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  project_id TEXT NOT NULL,
  chapter_no TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  prompt TEXT NOT NULL DEFAULT '',
  draft_content TEXT NOT NULL DEFAULT '',
  review_result TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  progress INTEGER NOT NULL DEFAULT 0,
  heartbeat_at TEXT,
  claimed_by TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(project_id) REFERENCES novel_projects(project_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_novel_jobs_project ON novel_generation_jobs(project_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_novel_jobs_status ON novel_generation_jobs(status, updated_at);

CREATE TABLE IF NOT EXISTS novel_audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT '',
  project_id TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '{}',
  success INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_novel_audit_project ON novel_audit_logs(project_id, created_at);

CREATE VIRTUAL TABLE IF NOT EXISTS novel_chapters_fts USING fts5(
  project_id UNINDEXED,
  chapter_no UNINDEXED,
  title,
  content,
  tokenize='unicode61'
);
CREATE TABLE IF NOT EXISTS novel_file_index (
  project_id TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  mtime_ns INTEGER NOT NULL DEFAULT 0,
  sha256 TEXT NOT NULL DEFAULT '',
  indexed_at TEXT NOT NULL,
  PRIMARY KEY(project_id, relative_path),
  FOREIGN KEY(project_id) REFERENCES novel_projects(project_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_novel_file_index_project ON novel_file_index(project_id, relative_path);
"""

# 已有库的增量迁移（新库直接由上面的 schema 建出，迁移语句对其幂等失败即跳过）
_MIGRATIONS = [
    # 执行器指令认领时间：支撑原子认领 + claimed 超时释放
    "ALTER TABLE executor_commands ADD COLUMN claimed_at TEXT",
    "ALTER TABLE executor_commands ADD COLUMN device_id TEXT DEFAULT ''",
    "ALTER TABLE executor_commands ADD COLUMN claim_token TEXT DEFAULT ''",
    "ALTER TABLE executor_commands ADD COLUMN lease_expires_at TEXT",
    "ALTER TABLE executor_commands ADD COLUMN result_late INTEGER DEFAULT 0",
    "ALTER TABLE reminders ADD COLUMN sending_token TEXT DEFAULT ''",
    "ALTER TABLE reminders ADD COLUMN sending_at TEXT",
    "ALTER TABLE reminders ADD COLUMN sending_lease_expires_at TEXT",
    # 检索自愈二期：自动抽取的原子占位键（老库补列，唯一索引由 schema 建）
    "ALTER TABLE auto_extract_log ADD COLUMN day_key TEXT DEFAULT ''",
    # 注：lessons.kind 由 _migrate_lessons 处理（要和去重重建一起做）
    # 关切主动追问时间：同一话题只主动问一次（问两遍就从关心变催促）
    "ALTER TABLE concerns ADD COLUMN asked_at TEXT",
    # 知识库分域（见建表注释：跨域污染实测 6/6 全错）
    "ALTER TABLE knowledge_chunks ADD COLUMN domain TEXT NOT NULL DEFAULT ''",
    # 被动目标追踪：goals 表长期为空不是因为用户没目标，是因为他从不打
    # "目标：XXX" 命令（同为命令式的 jargon/写作台账也全空）。改为从对话
    # 被动识别意向存为 candidate，问过两次没回应就自动丢弃。
    # source: command（用户显式创建）/ passive（从对话识别）
    "ALTER TABLE goals ADD COLUMN source TEXT NOT NULL DEFAULT 'command'",
    "ALTER TABLE goals ADD COLUMN asked_count INTEGER DEFAULT 0",
    "ALTER TABLE goals ADD COLUMN last_asked_at TEXT",
    # 使用反馈：被注入过几次 / 最近一次是什么时候。
    # importance 只增不减且被短句刷高（实测最高的是"你好""再确认一下"——
    # 越短越容易被检索命中），无法用来判断"这条到底有没有用"。
    # hit_count 记的是真实被采纳进 prompt 的次数，是淘汰噪声的可靠依据。
    "ALTER TABLE memories ADD COLUMN hit_count INTEGER DEFAULT 0",
    "ALTER TABLE memories ADD COLUMN last_hit_at TEXT",
    "ALTER TABLE lessons ADD COLUMN hit_count INTEGER DEFAULT 0",
    "ALTER TABLE lessons ADD COLUMN last_hit_at TEXT",
    "ALTER TABLE novel_generation_jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE novel_generation_jobs ADD COLUMN progress INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE novel_generation_jobs ADD COLUMN heartbeat_at TEXT",
    "ALTER TABLE novel_generation_jobs ADD COLUMN claimed_by TEXT NOT NULL DEFAULT ''",
]


def _column_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


def _backfill_user_column(conn: sqlite3.Connection, table: str) -> None:
    """为已有的简单用户表补列并把空主体归属主人。"""
    if not _table_exists(conn, table):
        return
    if not _column_exists(conn, table, "user_id"):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
    owner = settings.qq_admin_id.strip() or "owner"
    conn.execute(
        f"UPDATE {table} SET user_id=? WHERE user_id IS NULL OR user_id=''",
        (owner,),
    )


def _create_user_domain_indexes(conn: sqlite3.Connection) -> None:
    """用户列迁移完成后再创建索引，避免旧库建表脚本因缺列失败。"""
    statements = (
        "CREATE INDEX IF NOT EXISTS idx_worklog_user_date ON work_log(user_id, date, id)",
        "CREATE INDEX IF NOT EXISTS idx_reminders_user_due ON reminders(user_id, status, remind_at)",
        "CREATE INDEX IF NOT EXISTS idx_mood_user_created ON mood_log(user_id, created_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_lessons_user_created ON lessons(user_id, created_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_daily_summaries_user_date ON daily_summaries(user_id, date DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_weekly_reports_user_week ON weekly_reports(user_id, week DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_writing_user_created ON writing_log(user_id, created_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_fitness_user_created ON fitness_log(user_id, created_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_initiative_user_sent ON initiative_log(user_id, sent_at DESC, id DESC)",
    )
    for sql in statements:
        table = sql.split(" ON ", 1)[1].split("(", 1)[0]
        if _table_exists(conn, table):
            conn.execute(sql)


def _migrate_report_table(conn: sqlite3.Connection, table: str, period_col: str) -> None:
    """把日报/周报从全局唯一迁移为按主体唯一，并保留最新内容。"""
    if not _table_exists(conn, table):
        return
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    schema_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    schema_sql = "".join((schema_row[0] or "").lower().split()) if schema_row else ""
    unique_marker = f"unique(user_id,{period_col})"
    # weekly_reports 的 stats 是报表元数据，迁移时必须保留；即使某个中间版本
    # 已完成主体唯一约束但漏了该列，也要在这里补回来。
    if table == "weekly_reports" and "stats" not in cols:
        conn.execute("ALTER TABLE weekly_reports ADD COLUMN stats TEXT DEFAULT '{}'")
        cols.add("stats")
    if "user_id" in cols and unique_marker in schema_sql:
        _backfill_user_column(conn, table)
        return

    owner = settings.qq_admin_id.strip() or "owner"
    has_user_id = "user_id" in cols
    has_stats = "stats" in cols
    user_select = ", user_id" if has_user_id else ""
    stats_select = ", stats" if has_stats else ""
    rows = conn.execute(
        f"SELECT id, {period_col}, content, created_at{user_select}{stats_select} "
        f"FROM {table} ORDER BY id"
    ).fetchall()
    latest: dict[tuple[str, str], sqlite3.Row] = {}
    for row in rows:
        uid = (row["user_id"] if has_user_id else "") or owner
        period = str(row[period_col] or "")
        if not period:
            continue
        latest[(uid, period)] = row

    stats_definition = ",\n          stats TEXT DEFAULT '{}'" if table == "weekly_reports" else ""
    conn.execute(
        f"""CREATE TABLE {table}_new (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id TEXT NOT NULL DEFAULT '',
          {period_col} TEXT NOT NULL,
          content TEXT NOT NULL,
          created_at TEXT NOT NULL{stats_definition},
          UNIQUE(user_id, {period_col})
        )"""
    )
    for row in sorted(latest.values(), key=lambda item: item["id"]):
        uid = (row["user_id"] if has_user_id else "") or owner
        stats = (row["stats"] if has_stats else "{}") or "{}"
        if table == "weekly_reports":
            conn.execute(
                f"INSERT INTO {table}_new "
                f"(id, user_id, {period_col}, content, created_at, stats) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (row["id"], uid, row[period_col], row["content"], row["created_at"], stats),
            )
        else:
            conn.execute(
                f"INSERT INTO {table}_new (id, user_id, {period_col}, content, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (row["id"], uid, row[period_col], row["content"], row["created_at"]),
            )
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
    logger.info("%s 主体边界迁移完成：保留 %d 条按主体唯一记录", table, len(latest))


def _migrate_lessons(conn: sqlite3.Connection) -> None:
    """迁移教训表：主体隔离 + 去重 + 命中统计，且可重复执行。"""
    if not _table_exists(conn, "lessons"):
        return

    cols = {row[1] for row in conn.execute("PRAGMA table_info(lessons)").fetchall()}
    schema_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='lessons'"
    ).fetchone()
    schema_sql = "".join((schema_row[0] or "").lower().split()) if schema_row else ""
    required = {"user_id", "content", "context", "created_at", "kind", "hit_count", "last_hit_at"}
    if required.issubset(cols) and "unique(user_id,content)" in schema_sql:
        _backfill_user_column(conn, "lessons")
        return

    try:
        from app.services.self_reflect import classify_lesson
    except ImportError:
        def classify_lesson(_content: str) -> str:
            return "style"

    owner = settings.qq_admin_id.strip() or "owner"
    has_user_id = "user_id" in cols
    has_kind = "kind" in cols
    has_hit_count = "hit_count" in cols
    has_last_hit_at = "last_hit_at" in cols
    user_select = ", user_id" if has_user_id else ""
    kind_select = ", kind" if has_kind else ""
    hit_select = ", hit_count" if has_hit_count else ""
    last_hit_select = ", last_hit_at" if has_last_hit_at else ""
    rows = conn.execute(
        "SELECT id, content, context, created_at"
        f"{user_select}{kind_select}{hit_select}{last_hit_select} "
        "FROM lessons ORDER BY id"
    ).fetchall()

    # 同一主体同一内容只留最早一条；不同主体可以各有自己的同名教训。
    keep: dict[tuple[str, str], sqlite3.Row] = {}
    for row in rows:
        uid = (row["user_id"] if has_user_id else "") or owner
        key = (uid, row["content"])
        old = keep.get(key)
        if old is None or (row["created_at"], row["id"]) < (old["created_at"], old["id"]):
            keep[key] = row

    conn.execute(
        """CREATE TABLE lessons_new (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             user_id TEXT NOT NULL DEFAULT '',
             content TEXT NOT NULL,
             context TEXT DEFAULT '',
             created_at TEXT NOT NULL,
             kind TEXT NOT NULL DEFAULT 'style',
             hit_count INTEGER DEFAULT 0,
             last_hit_at TEXT,
             UNIQUE(user_id, content)
           )"""
    )
    for row in sorted(keep.values(), key=lambda item: item["id"]):
        uid = (row["user_id"] if has_user_id else "") or owner
        kind = row["kind"] if has_kind and row["kind"] else classify_lesson(row["content"])
        conn.execute(
            "INSERT INTO lessons_new "
            "(id, user_id, content, context, created_at, kind, hit_count, last_hit_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], uid, row["content"], row["context"] or "", row["created_at"],
                kind, row["hit_count"] if has_hit_count else 0,
                row["last_hit_at"] if has_last_hit_at else None,
            ),
        )
    conn.execute("DROP TABLE lessons")
    conn.execute("ALTER TABLE lessons_new RENAME TO lessons")
    logger.info("lessons 主体/去重迁移完成：保留 %d 条唯一教训", len(keep))


def _drop_legacy_fts(conn: sqlite3.Connection) -> None:
    """老 FTS 表缺 user_id → 先删，让 _SCHEMA 用新结构重建。

    两个坑逼出这个函数：①FTS5 虚表无法 ALTER 加列；②CREATE VIRTUAL TABLE
    IF NOT EXISTS 对"已存在但 schema 不匹配"的虚表仍会抛 OperationalError
    （IF NOT EXISTS 拦不住）——不先删的话整个 executescript 中途失败回滚，
    连增量迁移都跑不到。数据无损：memories 原表不动，_fts_backfill 重建索引。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'"
    ).fetchone()
    if row and "user_id" not in (row[0] or ""):
        conn.execute("DROP TABLE memories_fts")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _legacy_event_id(row) -> str:
    """按存量字段生成稳定事件键，供无 event_id 的旧客户端迁移。"""
    import hashlib
    import json

    payload = {
        "kind": row["kind"] or "",
        "name": row["name"] or "",
        "detail": row["detail"] or "",
        "start_ts": row["start_ts"] or "",
        "end_ts": row["end_ts"] or "",
        "meta": row["meta"] or "{}",
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _migrate_behavior_events(conn: sqlite3.Connection) -> None:
    """补齐事件主体、幂等键、规范化旧 meta，并创建部分唯一索引。"""
    if not _table_exists(conn, "behavior_events"):
        return
    if not _column_exists(conn, "behavior_events", "user_id"):
        conn.execute("ALTER TABLE behavior_events ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
    if not _column_exists(conn, "behavior_events", "event_id"):
        conn.execute("ALTER TABLE behavior_events ADD COLUMN event_id TEXT")
    owner = settings.qq_admin_id.strip() or "owner"
    conn.execute("UPDATE behavior_events SET user_id=? WHERE user_id='' OR user_id IS NULL", (owner,))

    # 旧库没有 event_id：按 id 顺序回填；碰到重复键保留第一条，避免唯一索引
    # 因历史重放数据无法创建，且与新的 ON CONFLICT 语义保持一致。
    seen: set[str] = set()
    rows = conn.execute(
        "SELECT id, event_id, kind, name, detail, start_ts, end_ts, meta "
        "FROM behavior_events ORDER BY id"
    ).fetchall()
    for row in rows:
        event_id = (row["event_id"] or "").strip() or _legacy_event_id(row)
        if event_id in seen:
            conn.execute("DELETE FROM behavior_events WHERE id=?", (row["id"],))
            continue
        seen.add(event_id)
        conn.execute("UPDATE behavior_events SET event_id=? WHERE id=?", (event_id, row["id"]))

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_behavior_user_start "
        "ON behavior_events(user_id, start_ts)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_behavior_event_id "
        "ON behavior_events(event_id) WHERE event_id IS NOT NULL AND event_id <> ''"
    )


def _migrate_user_id(conn: sqlite3.Connection) -> None:
    """v0.4 多人支持：8 张用户态表加 user_id 列，老数据回填主人身份。

    简单加列用于无主键/唯一约束冲突的表；单列主键（profile/concerns/
    jargon_terms）与三列唯一约束（facts）必须重建表。全部幂等：
    有 user_id 列即跳过，回填只碰 user_id='' 的行；表缺失则跳过
    （部分建成的残库也能迁，不因一张表缺位全盘失败）。
    主人身份 = settings.qq_admin_id（QQ 推送已配），未配置回退 'owner'。
    """
    owner = settings.qq_admin_id.strip() or "owner"

    # ① 简单加列（代理主键表；表缺失跳过）
    for t in ("memories", "goals", "unresolved_issues", "style_examples", "llm_usage"):
        if _table_exists(conn, t) and not _column_exists(conn, t, "user_id"):
            conn.execute(
                f"ALTER TABLE {t} ADD COLUMN user_id TEXT NOT NULL DEFAULT ''"
            )

    # ② 重建型：facts（UNIQUE 三列 → 四列）
    if _table_exists(conn, "facts") and not _column_exists(conn, "facts", "user_id"):
        conn.execute(
            """CREATE TABLE facts_new (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id TEXT NOT NULL DEFAULT '',
              subject TEXT NOT NULL,
              predicate TEXT NOT NULL,
              object TEXT NOT NULL,
              source_memory_id INTEGER,
              confidence REAL DEFAULT 0.7,
              updated_at TEXT NOT NULL,
              UNIQUE(user_id, subject, predicate, object)
            )"""
        )
        conn.execute(
            """INSERT INTO facts_new (id, subject, predicate, object, source_memory_id,
                                      confidence, updated_at)
               SELECT id, subject, predicate, object, source_memory_id,
                      confidence, updated_at FROM facts"""
        )
        conn.execute("DROP TABLE facts")
        conn.execute("ALTER TABLE facts_new RENAME TO facts")

    # ③ 重建型：profile（主键 → (user_id, dimension)）
    if _table_exists(conn, "profile") and not _column_exists(conn, "profile", "user_id"):
        conn.execute("""CREATE TABLE profile_new (
              user_id TEXT NOT NULL DEFAULT '', dimension TEXT NOT NULL,
              value TEXT NOT NULL, confidence REAL DEFAULT 0.5,
              updated_at TEXT NOT NULL, PRIMARY KEY(user_id, dimension))""")
        conn.execute("INSERT INTO profile_new (dimension, value, confidence, updated_at) SELECT dimension, value, confidence, updated_at FROM profile")
        conn.execute("DROP TABLE profile")
        conn.execute("ALTER TABLE profile_new RENAME TO profile")

    # ④ 重建型：concerns（主键 → (user_id, topic)）
    if _table_exists(conn, "concerns") and not _column_exists(conn, "concerns", "user_id"):
        conn.execute("""CREATE TABLE concerns_new (
              user_id TEXT NOT NULL DEFAULT '', topic TEXT NOT NULL,
              mention_count INTEGER DEFAULT 1, last_mentioned_at TEXT NOT NULL,
              PRIMARY KEY(user_id, topic))""")
        conn.execute("INSERT INTO concerns_new (topic, mention_count, last_mentioned_at) SELECT topic, mention_count, last_mentioned_at FROM concerns")
        conn.execute("DROP TABLE concerns")
        conn.execute("ALTER TABLE concerns_new RENAME TO concerns")

    # ⑤ 重建型：jargon_terms（主键 → (user_id, term)）
    if _table_exists(conn, "jargon_terms") and not _column_exists(conn, "jargon_terms", "user_id"):
        conn.execute("""CREATE TABLE jargon_terms_new (
              user_id TEXT NOT NULL DEFAULT '', term TEXT NOT NULL,
              explanation TEXT NOT NULL, created_at TEXT NOT NULL,
              times_used INTEGER DEFAULT 0, PRIMARY KEY(user_id, term))""")
        conn.execute("INSERT INTO jargon_terms_new (term, explanation, created_at, times_used) SELECT term, explanation, created_at, times_used FROM jargon_terms")
        conn.execute("DROP TABLE jargon_terms")
        conn.execute("ALTER TABLE jargon_terms_new RENAME TO jargon_terms")

    # ⑥ 老数据回填主人身份（幂等：只碰 user_id='' 的行；表缺失跳过）
    for t in ("memories", "facts", "profile", "concerns", "jargon_terms",
              "style_examples", "goals", "unresolved_issues", "llm_usage"):
        if _table_exists(conn, t):
            conn.execute(
                f"UPDATE {t} SET user_id = ? WHERE user_id IS NULL OR user_id = ''",
                (owner,),
            )

    # ⑦ 依赖 user_id 的索引（加列之后再建，避免老库 executescript 中途炸）
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id, id)")
    if _table_exists(conn, "llm_usage"):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_user ON llm_usage(user_id, created_at)")

    # ⑧ 兜底重建 FTS 表：主 schema 脚本若因老库中途失败，FTS 可能漏建；
    #    IF NOT EXISTS 幂等，数据由 init_db 末尾的 _fts_backfill 回填
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5("
        "memory_id UNINDEXED, user_id UNINDEXED, grams)"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5("
        "chunk_id UNINDEXED, grams)"
    )

# 向量表（sqlite-vec 虚拟表）。
# 单独拆分：扩展不可用时 init_db 跳过此段，基础功能不受影响。
# 维度跟随 .env 的 EMBEDDING_DIMENSION（不同向量模型维度不同：1024 / 2048）。
VEC_TABLE_SQL = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS memory_vectors USING vec0(
  memory_id INTEGER PRIMARY KEY,
  embedding FLOAT[{settings.embedding_dimension}] distance_metric=cosine
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vectors USING vec0(
  chunk_id INTEGER PRIMARY KEY,
  embedding FLOAT[{settings.embedding_dimension}] distance_metric=cosine
);
"""

# 记忆全文索引（FTS5）：gram 化文本 + id 映射，替代检索时的
# Python 全表扫描。unicode61 对中文不分词，gram 化在应用层做。
FTS_TABLE_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  memory_id UNINDEXED,
  user_id UNINDEXED,   -- 按用户隔离检索：查询时 WHERE user_id = ?
  grams
);
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
  chunk_id UNINDEXED,
  grams
);
"""

_SCHEMA = _BASE_SCHEMA + VEC_TABLE_SQL + FTS_TABLE_SQL
_SCHEMA_NO_VEC = _BASE_SCHEMA + FTS_TABLE_SQL


def _is_vec_unavailable_error(exc: BaseException) -> bool:
    """只把明确的 sqlite-vec 缺失/不可加载错误视为可降级。"""
    if isinstance(exc, (ImportError, OSError)):
        return True
    message = str(exc).casefold()
    markers = (
        "no such module: vec",
        "no such module: vec0",
        "sqlite_vec",
        "sqlite-vec",
        "loadable_path",
        "cannot open shared object",
        "cannot open dynamic library",
        "undefined symbol",
        "dlopen",
    )
    return any(marker in message for marker in markers)


def _execute_script(conn: sqlite3.Connection, script: str) -> None:
    """在当前事务内逐条执行 SQL，避免 executescript 隐式提交事务。"""
    statement: list[str] = []
    for line in script.splitlines(keepends=True):
        statement.append(line)
        text = "".join(statement)
        if not sqlite3.complete_statement(text):
            continue
        sql = text.strip()
        statement = []
        if sql:
            conn.execute(sql)
    trailing = "".join(statement).strip()
    if trailing:
        conn.execute(trailing)


_vec_state: bool | None = None  # 上次扩展加载结果（None=尚未打过日志），避免每次连接刷屏

# 线程本地连接缓存：一次聊天请求会开 20+ 连接（十几个注入器各开各的），
# 每个连接都重跑 WAL pragma + 加载 sqlite-vec 扩展，是纯开销。
# SQLite 连接不可跨线程，threading.local 正好每线程一条长驻连接。
import threading
from contextlib import contextmanager

_local = threading.local()


class _CachedConnection(sqlite3.Connection):
    """关闭时同步清理线程本地缓存，避免留下悬空连接句柄。"""

    def close(self) -> None:
        try:
            super().close()
        finally:
            if getattr(_local, "conn", None) is self:
                _local.conn = None
                _local.db_path = None


def connect() -> sqlite3.Connection:
    """取当前线程的数据库连接：WAL、busy timeout、外键和 sqlite-vec。

    sqlite-vec 缺失时只关闭向量能力；数据库连接、schema 和其他真实错误不吞。
    """
    global _vec_state
    db_file = settings.db_file.resolve()
    conn = getattr(_local, "conn", None)
    cached_path = getattr(_local, "db_path", None)
    if conn is not None and cached_path == str(db_file):
        try:
            conn.execute("SELECT 1")
            conn.execute("PRAGMA foreign_keys=ON")
            return conn
        except sqlite3.ProgrammingError:
            _local.conn = None
        except sqlite3.OperationalError:
            try:
                conn.close()
            except sqlite3.Error as exc:
                logger.debug("关闭失效 SQLite 连接时忽略异常: %s", exc)
            _local.conn = None
    elif conn is not None:
        try:
            conn.close()
        except sqlite3.Error as exc:
            logger.debug("切换 SQLite 数据库时忽略关闭异常: %s", exc)
        _local.conn = None

    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_file), timeout=5.0, factory=_CachedConnection)
    conn.row_factory = sqlite3.Row
    # foreign_keys 必须在事务开始前打开；以后每次复用也重新确认一次。
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    extension_enabled = False
    try:
        # 让 sqlite-vec 包自己给出扩展文件路径（Linux/Windows 通用）。
        import sqlite_vec

        conn.enable_load_extension(True)
        extension_enabled = True
        loadable_path = sqlite_vec.loadable_path()
        conn.load_extension(loadable_path)
        if _vec_state is not True:
            logger.info("sqlite-vec 已加载: %s", loadable_path)
        _vec_state = True
    except (ImportError, OSError) as exc:
        if _vec_state is not False:
            logger.warning("sqlite-vec 不可用，向量检索已禁用: %s", exc)
        _vec_state = False
    except sqlite3.OperationalError as exc:
        if not _is_vec_unavailable_error(exc):
            conn.close()
            raise
        if _vec_state is not False:
            logger.warning("sqlite-vec 不可用，向量检索已禁用: %s", exc)
        _vec_state = False
    except sqlite3.Error:
        conn.close()
        raise
    finally:
        # load_extension 失败也必须关闭扩展加载能力，避免连接继续暴露加载入口。
        if extension_enabled:
            try:
                conn.enable_load_extension(False)
            except sqlite3.Error:
                # 真实错误已经在上面的分支抛出；这里仅避免二次异常覆盖原错误。
                pass
    _local.conn = conn
    _local.db_path = str(db_file)
    return conn


@contextmanager
def db_connection():
    """统一连接上下文：yield 当前线程的缓存连接，退出时不真正关闭
    （连接属于缓存，close 只会丢失复用）。

    推荐新代码用 `with db_connection() as conn:` 替代手写 try/finally
    connect+close——语义更清晰，也不会误触缓存的重建开销。旧写法仍兼容。
    """
    conn = connect()
    try:
        yield conn
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


def reset_connections() -> None:
    """丢弃当前线程的本地连接缓存（测试切换 db_path 后必须调用）。"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    _local.conn = None
    _local.db_path = None


def _run_integrity_checks(conn: sqlite3.Connection) -> None:
    """迁移后验证 SQLite 内部结构与外键，不把真实错误降级成启动成功。"""
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise sqlite3.DatabaseError("SQLite foreign_keys 未启用")
    integrity = [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()]
    if integrity != ["ok"]:
        raise sqlite3.DatabaseError("SQLite integrity_check 失败: " + "; ".join(map(str, integrity[:5])))
    foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        raise sqlite3.DatabaseError(
            f"SQLite foreign_key_check 失败：{len(foreign_keys)} 个约束违规"
        )


def init_db() -> None:
    """创建/迁移数据库，并在真实错误时 fail-fast。

    基础 schema 先单独提交，保证新库在后续增量迁移失败时仍是完整可检查的
    基线；所有增量迁移、数据回填和 schema_version 更新都在同一事务内，
    失败时不会留下半个迁移列/索引。
    """
    conn = connect()
    try:
        # ① 幂等建立完整基础结构。基础结构本身不是增量迁移，提交后才进入
        # 迁移事务；这样失败测试和运维排障仍可查询 schema_version 表。
        conn.execute("BEGIN IMMEDIATE")
        _drop_legacy_fts(conn)
        try:
            _execute_script(conn, _SCHEMA_NO_VEC if _vec_state is False else _SCHEMA)
        except sqlite3.OperationalError as exc:
            # 只有明确的 sqlite-vec 虚表缺失才允许重来一次无向量 schema；
            # 任意 schema 冲突、磁盘/锁错误都必须抛出。
            if not _is_vec_unavailable_error(exc):
                raise
            conn.rollback()
            conn.execute("BEGIN IMMEDIATE")
            _drop_legacy_fts(conn)
            _execute_script(conn, _SCHEMA_NO_VEC)
            logger.warning("sqlite-vec 不可用，向量检索已禁用: %s", exc)
        conn.commit()

        # ② 所有增量迁移放入单一事务，任何一步失败均回滚。
        conn.execute("BEGIN IMMEDIATE")
        # 顺序有讲究：_migrate_user_id 会整表重建 concerns，必须先跑，
        # 之后再加 asked_at，避免刚加的列被重建丢失。
        _migrate_user_id(conn)
        _migrate_behavior_events(conn)
        # 用户域旧表先补主体列；日报/周报需要重建唯一约束，避免不同主体
        # 共享同一个日期/周次时互相覆盖。
        for table in ("work_log", "reminders", "mood_log", "writing_log", "fitness_log", "initiative_log"):
            _backfill_user_column(conn, table)
        _migrate_report_table(conn, "daily_summaries", "date")
        _migrate_report_table(conn, "weekly_reports", "week")
        for sql in _MIGRATIONS:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).casefold():
                    raise
        _migrate_lessons(conn)
        _create_user_domain_indexes(conn)
        conn.execute(
            "INSERT INTO schema_version (id, version, applied_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET version=excluded.version, applied_at=excluded.applied_at",
            (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
        )
        _run_integrity_checks(conn)
        conn.commit()
    except (sqlite3.Error, ValueError, ImportError):
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        logger.exception("数据库初始化/迁移失败，已回滚，启动终止")
        raise
    finally:
        conn.close()
        _local.conn = None
        _local.db_path = None

    # FTS 存量回填：FTS 表空而有数据（老库升级）时一次性补索引。
    # FTS 是基础检索结构，回填真实失败不能再被吞成“启动成功”。
    for backfill in ("app.core.memory:_fts_backfill", "app.core.knowledge:_fts_backfill"):
        mod_name, fn_name = backfill.split(":")
        mod = __import__(mod_name, fromlist=[fn_name])
        conn = connect()
        try:
            getattr(mod, fn_name)(conn)
        finally:
            conn.close()
            _local.conn = None
            _local.db_path = None
