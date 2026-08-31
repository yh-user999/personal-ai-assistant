"""SQLite 数据层：建表 + sqlite-vec 向量表。

表结构与 docs/实施方案细则.md 第四节对应。
注意：sqlite-vec 是扩展，需在连接时 load_extension。
"""
import logging
import sqlite3

from app.config import settings

logger = logging.getLogger("assistant.db")

_BASE_SCHEMA = """
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
-- 注意：idx_memories_user 依赖 user_id 列，老库迁移时在 _migrate_user_id 里
-- 加列后再建（放这里会让老库的 executescript 中途炸掉）
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

-- ⑨ 关切话题（用户最近在意的主题，mention_count 追踪活跃度，按用户隔离）
CREATE TABLE IF NOT EXISTS concerns (
  user_id TEXT NOT NULL DEFAULT '',
  topic TEXT NOT NULL,
  mention_count INTEGER DEFAULT 1,
  last_mentioned_at TEXT NOT NULL,
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
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, remind_at);

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


def _column_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


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
    for t in ("memories", "goals", "unresolved_issues", "style_examples"):
        try:
            if _table_exists(conn, t) and not _column_exists(conn, t, "user_id"):
                conn.execute(
                    f"ALTER TABLE {t} ADD COLUMN user_id TEXT NOT NULL DEFAULT ''"
                )
        except sqlite3.OperationalError:
            pass

    # ② 重建型：facts（UNIQUE 三列 → 四列）
    if _table_exists(conn, "facts") and not _column_exists(conn, "facts", "user_id"):
        conn.executescript(
            """
            CREATE TABLE facts_new (
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
            INSERT INTO facts_new (id, subject, predicate, object, source_memory_id,
                                   confidence, updated_at)
              SELECT id, subject, predicate, object, source_memory_id,
                     confidence, updated_at FROM facts;
            DROP TABLE facts;
            ALTER TABLE facts_new RENAME TO facts;
            """
        )

    # ③ 重建型：profile（主键 → (user_id, dimension)）
    if _table_exists(conn, "profile") and not _column_exists(conn, "profile", "user_id"):
        conn.executescript(
            """
            CREATE TABLE profile_new (
              user_id TEXT NOT NULL DEFAULT '',
              dimension TEXT NOT NULL,
              value TEXT NOT NULL,
              confidence REAL DEFAULT 0.5,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(user_id, dimension)
            );
            INSERT INTO profile_new (dimension, value, confidence, updated_at)
              SELECT dimension, value, confidence, updated_at FROM profile;
            DROP TABLE profile;
            ALTER TABLE profile_new RENAME TO profile;
            """
        )

    # ④ 重建型：concerns（主键 → (user_id, topic)）
    if _table_exists(conn, "concerns") and not _column_exists(conn, "concerns", "user_id"):
        conn.executescript(
            """
            CREATE TABLE concerns_new (
              user_id TEXT NOT NULL DEFAULT '',
              topic TEXT NOT NULL,
              mention_count INTEGER DEFAULT 1,
              last_mentioned_at TEXT NOT NULL,
              PRIMARY KEY(user_id, topic)
            );
            INSERT INTO concerns_new (topic, mention_count, last_mentioned_at)
              SELECT topic, mention_count, last_mentioned_at FROM concerns;
            DROP TABLE concerns;
            ALTER TABLE concerns_new RENAME TO concerns;
            """
        )

    # ⑤ 重建型：jargon_terms（主键 → (user_id, term)）
    if _table_exists(conn, "jargon_terms") and not _column_exists(conn, "jargon_terms", "user_id"):
        conn.executescript(
            """
            CREATE TABLE jargon_terms_new (
              user_id TEXT NOT NULL DEFAULT '',
              term TEXT NOT NULL,
              explanation TEXT NOT NULL,
              created_at TEXT NOT NULL,
              times_used INTEGER DEFAULT 0,
              PRIMARY KEY(user_id, term)
            );
            INSERT INTO jargon_terms_new (term, explanation, created_at, times_used)
              SELECT term, explanation, created_at, times_used FROM jargon_terms;
            DROP TABLE jargon_terms;
            ALTER TABLE jargon_terms_new RENAME TO jargon_terms;
            """
        )

    # ⑥ 老数据回填主人身份（幂等：只碰 user_id='' 的行；表缺失跳过）
    for t in ("memories", "facts", "profile", "concerns", "jargon_terms",
              "style_examples", "goals", "unresolved_issues"):
        try:
            if _table_exists(conn, t):
                conn.execute(
                    f"UPDATE {t} SET user_id = ? WHERE user_id = ''", (owner,)
                )
        except sqlite3.OperationalError:
            pass

    # ⑦ 依赖 user_id 的索引（加列之后再建，避免老库 executescript 中途炸）
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id, id)")

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


_vec_state: bool | None = None  # 上次扩展加载结果（None=尚未打过日志），避免每次连接刷屏

# 线程本地连接缓存：一次聊天请求会开 20+ 连接（十几个注入器各开各的），
# 每个连接都重跑 WAL pragma + 加载 sqlite-vec 扩展，是纯开销。
# SQLite 连接不可跨线程，threading.local 正好每线程一条长驻连接。
import threading  # noqa: E402
from contextlib import contextmanager  # noqa: E402

_local = threading.local()


def connect() -> sqlite3.Connection:
    """取当前线程的数据库连接（长驻复用）：WAL + busy_timeout + sqlite-vec。

    同线程内所有调用共享一条连接——省掉每请求 20+ 次建连/加载扩展的开销。
    注意：调用方沿用既有 `finally: conn.close()` 的写法也无妨（close 后
    下次 connect 会自动重建）；新代码可以不关。
    事务语义：长驻连接上多次 commit 互不干扰，与逐条连接行为一致。
    """
    global _vec_state
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.ProgrammingError:
            # 连接已被 close()：重建
            _local.conn = None
        except sqlite3.OperationalError:
            # 连接损坏：重建
            try:
                conn.close()
            except Exception:
                pass
            _local.conn = None

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
    _local.conn = conn
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
    except Exception:
        conn.rollback()
        raise


def reset_connections() -> None:
    """丢弃所有线程的本地连接缓存（测试切换 db_path 后必须调用，否则
    长驻连接仍指向旧库）。"""
    _local.conn = None


def init_db() -> None:
    conn = connect()
    try:
        _drop_legacy_fts(conn)  # 老 FTS 缺 user_id 先删（虚表 schema 冲突会炸 executescript）
        conn.executescript(_SCHEMA)
    except sqlite3.OperationalError as e:
        # sqlite-vec 未安装时虚拟表建表失败：回滚，改用基础表（向量检索自动退化）
        conn.rollback()
        try:
            _drop_legacy_fts(conn)
            conn.executescript(_SCHEMA.replace(VEC_TABLE_SQL, ""))
            logger.warning("sqlite-vec 不可用，向量检索已禁用: %s", e)
        except sqlite3.OperationalError:
            pass
    # 增量迁移：两条建表路径（vec 可用/降级）都要跑——老库升级加列与 user_id 迁移
    try:
        for sql in _MIGRATIONS:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # 列已存在（新库由 schema 直接建出）
        _migrate_user_id(conn)  # v0.4 多人支持：老库加 user_id 并回填
        conn.commit()
    except sqlite3.OperationalError as e:
        # 极端情况（连基础表都没建成）下放弃迁移，功能退化为 v0.3 行为
        conn.rollback()
        logger.warning("增量迁移失败（基础表缺失？）: %s", e)
    finally:
        conn.close()

    # FTS 存量回填：FTS 表空而有数据（老库升级）时一次性补索引
    for backfill in ("app.core.memory:_fts_backfill", "app.core.knowledge:_fts_backfill"):
        try:
            mod_name, fn_name = backfill.split(":")
            mod = __import__(mod_name, fromlist=[fn_name])
            conn = connect()
            try:
                getattr(mod, fn_name)(conn)
            finally:
                conn.close()
        except Exception as e:
            logger.warning("FTS 回填失败 %s（检索退化为空候选）: %s", backfill, e)
