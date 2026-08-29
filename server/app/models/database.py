"""SQLite 数据层：建表 + sqlite-vec 向量表。

表结构与 docs/实施方案细则.md 第四节对应。
注意：sqlite-vec 是扩展，需在连接时 load_extension。
"""
import logging
import sqlite3
from pathlib import Path

from app.config import settings

logger = logging.getLogger("assistant.db")

_BASE_SCHEMA = """
-- ① 对话记忆（情境记忆）
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sender TEXT NOT NULL,               -- 'user' / 'assistant'
  content TEXT NOT NULL,
  summary TEXT DEFAULT '',
  topics TEXT DEFAULT '[]',           -- JSON 数组
  ts TEXT NOT NULL,
  importance REAL DEFAULT 1.0
);

-- ② 事实表（永久知识，三元组）
CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject TEXT NOT NULL,
  predicate TEXT NOT NULL,
  object TEXT NOT NULL,
  source_memory_id INTEGER,
  confidence REAL DEFAULT 0.7,
  updated_at TEXT NOT NULL,
  UNIQUE(subject, predicate, object)
);

-- ③ 画像表（四维度）
CREATE TABLE IF NOT EXISTS profile (
  dimension TEXT PRIMARY KEY,         -- technical_background / work_habit / learning_rhythm / project_info
  value TEXT NOT NULL,
  confidence REAL DEFAULT 0.5,
  updated_at TEXT NOT NULL
);

-- ④ 工作日志（手动记录）
CREATE TABLE IF NOT EXISTS work_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
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
  week TEXT NOT NULL UNIQUE,          -- '2025-W25'
  content TEXT NOT NULL,
  stats TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);

-- 查询索引（v0.3 采纳评审建议：常用查询字段建索引）
CREATE INDEX IF NOT EXISTS idx_memories_ts ON memories(ts);
CREATE INDEX IF NOT EXISTS idx_facts_updated ON facts(updated_at);
CREATE INDEX IF NOT EXISTS idx_worklog_created ON work_log(created_at);

-- ⑦ 教训表（自省模块：用户纠正的内容，高优先级长期记忆）
CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content TEXT NOT NULL,            -- 用户纠正的原话
  context TEXT DEFAULT '',          -- 被纠正的 AI 回复（上下文）
  created_at TEXT NOT NULL
);

-- ⑧ 每日小结（每晚 22:00 生成当天工作摘要）
CREATE TABLE IF NOT EXISTS daily_summaries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL UNIQUE,        -- '2026-08-25'
  content TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- ⑨ 关切话题（用户最近在意的主题，mention_count 追踪活跃度）
CREATE TABLE IF NOT EXISTS concerns (
  topic TEXT PRIMARY KEY,
  mention_count INTEGER DEFAULT 1,
  last_mentioned_at TEXT NOT NULL
);

-- ⑩ 术语词典（用户问过的技术名词 → 解释，保证口径一致）
CREATE TABLE IF NOT EXISTS jargon_terms (
  term TEXT PRIMARY KEY,
  explanation TEXT NOT NULL,
  created_at TEXT NOT NULL,
  times_used INTEGER DEFAULT 0
);

-- ⑪ 风格范例（用户满意的回复，few-shot 注入）
CREATE TABLE IF NOT EXISTS style_examples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- ⑫ 知识库块（RAG：文档切块后的文本，与 chunk_vectors 一一对应）
CREATE TABLE IF NOT EXISTS knowledge_chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_name TEXT NOT NULL,           -- 来源文档名
  chunk_index INTEGER NOT NULL,     -- 在文档中的块序号
  content TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON knowledge_chunks(doc_name);

-- ⑬ 生成的文档（对话式"写文档"保存的产物，同时同步进知识库）
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- ⑭ 目标（Goal 系统：用户的目标与进度，周报核对）
CREATE TABLE IF NOT EXISTS goals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  status TEXT DEFAULT 'active',    -- active / done / paused
  progress TEXT DEFAULT '',        -- 进度备注
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- ⑮ 未解决问题（unresolved：聊到一半被打断的话题）
CREATE TABLE IF NOT EXISTS unresolved_issues (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
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
  status TEXT DEFAULT 'pending',   -- pending / claimed / done / failed
  result TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  claimed_at TEXT,
  executed_at TEXT
);

-- ⑰ 小说设定卡（第 6.19 课：策划数据，人物/事件权威事实）
CREATE TABLE IF NOT EXISTS novel_facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book TEXT NOT NULL,              -- 所属书（如 小说-寂静杀戮）
  keywords TEXT NOT NULL,          -- 触发词（逗号分隔：人物名/事件词）
  content TEXT NOT NULL,           -- 设定条目正文
  created_at TEXT NOT NULL
);

-- ⑱ 定时提醒（第 6.24 课：机器人主动触达的最小通道）
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content TEXT NOT NULL,           -- 提醒内容
  remind_at TEXT NOT NULL,         -- 提醒时间（ISO, UTC）
  status TEXT DEFAULT 'pending',   -- pending / notified / cancelled
  created_at TEXT NOT NULL
);

-- ⑲ 写作台账（第 6.25 课：小说写作增强）
CREATE TABLE IF NOT EXISTS writing_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chapter TEXT,                    -- 章节号（可空）
  words INTEGER NOT NULL,          -- 本次字数
  created_at TEXT NOT NULL
);

-- ⑳ 情绪日志（第 6.27 课：情绪记忆层 + 反馈闭环）
CREATE TABLE IF NOT EXISTS mood_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mood TEXT NOT NULL,              -- 疲惫 / 着急 / 烦躁 / 开心 / 低落
  snippet TEXT DEFAULT '',         -- 触发消息摘要（≤80 字）
  created_at TEXT NOT NULL
);

-- ㉑ 健身台账（第 6.29 课：健身减脂助手）
CREATE TABLE IF NOT EXISTS fitness_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,              -- weight / training
  value REAL,                      -- 体重数值（训练记录为空）
  detail TEXT DEFAULT '',          -- 训练内容描述
  created_at TEXT NOT NULL
);

-- ㉒ 健身知识卡（第 6.29 课：权威指南提炼，仿小说设定卡）
CREATE TABLE IF NOT EXISTS fitness_facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book TEXT NOT NULL,              -- 固定 '健身'
  keywords TEXT NOT NULL,          -- 触发词（逗号分隔）
  content TEXT NOT NULL,           -- 权威条目正文（含出处年份）
  created_at TEXT NOT NULL
);
"""

# 已有库的增量迁移（新库直接由上面的 schema 建出，迁移语句对其幂等失败即跳过）
_MIGRATIONS = [
    # 执行器指令认领时间：支撑原子认领 + claimed 超时释放
    "ALTER TABLE executor_commands ADD COLUMN claimed_at TEXT",
]

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

_SCHEMA = _BASE_SCHEMA + VEC_TABLE_SQL


_vec_state: bool | None = None  # 上次扩展加载结果（None=尚未打过日志），避免每次连接刷屏


def connect() -> sqlite3.Connection:
    """打开数据库连接：WAL 模式（读写并发）+ busy_timeout（锁等待）。

    WAL 允许读写并行：采集事件写入不会阻塞记忆检索查询。
    """
    global _vec_state
    db_file = settings.db_file
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_file), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        # 让 sqlite-vec 包自己给出扩展文件路径（Linux/Windows 通用）。
        # 之前直接 load_extension("vec0") 按名字找，Linux 上找不到文件会失败。
        import sqlite_vec

        conn.enable_load_extension(True)
        conn.load_extension(sqlite_vec.loadable_path())
        if _vec_state is not True:
            logger.info("sqlite-vec 已加载: %s", sqlite_vec.loadable_path())
        _vec_state = True
    except Exception as e:
        # 扩展未加载不致命：向量检索功能暂不可用，其余功能正常
        if _vec_state is not False:
            logger.warning("sqlite-vec 不可用，向量检索已禁用: %s", e)
        _vec_state = False
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
        for sql in _MIGRATIONS:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # 列已存在（新库由 schema 直接建出）
        conn.commit()
    except sqlite3.OperationalError as e:
        # sqlite-vec 未安装时虚拟表建表失败：回滚，改用基础表（向量检索自动退化）
        conn.rollback()
        try:
            conn.executescript(_SCHEMA.replace(VEC_TABLE_SQL, ""))
            conn.commit()
            logger.warning("sqlite-vec 不可用，向量检索已禁用: %s", e)
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()
