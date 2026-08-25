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
"""

# 向量表（sqlite-vec 虚拟表，与 memories.id 关联）。
# 单独拆分：扩展不可用时 init_db 跳过此段，基础功能不受影响。
# 维度跟随 .env 的 EMBEDDING_DIMENSION（不同向量模型维度不同：1024 / 2048）。
VEC_TABLE_SQL = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS memory_vectors USING vec0(
  memory_id INTEGER PRIMARY KEY,
  embedding FLOAT[{settings.embedding_dimension}] distance_metric=cosine
);
"""

_SCHEMA = _BASE_SCHEMA + VEC_TABLE_SQL


def connect() -> sqlite3.Connection:
    """打开数据库连接：WAL 模式（读写并发）+ busy_timeout（锁等待）。

    WAL 允许读写并行：采集事件写入不会阻塞记忆检索查询。
    """
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
        logger.info("sqlite-vec 已加载: %s", sqlite_vec.loadable_path())
    except Exception as e:
        # 扩展未加载不致命：向量检索功能暂不可用，其余功能正常
        logger.warning("sqlite-vec 不可用，向量检索已禁用: %s", e)
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
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
