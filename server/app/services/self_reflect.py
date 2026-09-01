"""自省模块：检测用户纠正 → 存教训（长期记忆）→ 聊天时注入 → 周报统计。

对应身份定义行为规范："禁止忽视用户的历史选择和风格偏好"。
纠正信号词命中即视为纠正（简单可靠，不额外调 LLM）。

去重（实测修复）：老实现每次直接 INSERT，真实库 52 行里只有 7 条不同内容，
LIMIT 5 的注入窗口被同一句话的副本占满，"你就叫小月吧"这条定义她身份的
设定反而被挤出 prompt。现在 content 唯一，重复纠正只刷新时间。

优先级：identity 类（起名/身份设定）永远排在最前且不占普通配额——
"你叫小月"是人格锚点，不该和"重排序放检索之后"这种技术细节抢窗口。
"""
import re
from datetime import datetime, timezone

from app.models.database import connect

# 纠正信号（中文口语常见表达；命中任一词即判定）
# 含身份设定类："给你起名 X / 你叫 X / 你的名字是 X"
CORRECTION_PATTERNS = (
    "不对", "错了", "不是这样", "应该是", "记住", "以后", "纠正",
    "别说", "不要再", "别再说", "你应该", "要记住",
    # 身份设定类（"你就叫"原先漏了——"你就叫小狗吧"整句检测不到，
    # 既进不了 lessons 也绕过身份守卫）
    "起名", "名字叫", "就叫你", "你的名字", "叫你", "你就叫",
)

# 身份设定类信号：给她起名、定身份、定自称——永久最高优先级
IDENTITY_PATTERN = re.compile(
    r"起名|名字叫|就叫你|你的名字|叫你|你就叫|自称|你是我的|以后你(?:就)?是"
)

# 疑问句：用户在**问**身份，不是在**定**身份。
# 实测线上把"还记得你的名字吗"存成了 identity 并永久最高优先注入——
# 一句提问占住了人格锚点的位置。
#
# 判据只认**询问式开头**，不认句末语气词。原因：中文命名句常带确认尾缀，
# "你就叫小月吧，记住了吗"整句是命名而非提问，靠句末"吗"判断会把它误杀
# （实测就杀掉了用户真正给她起名的那条）。
QUESTION_PATTERN = re.compile(
    r"^\s*(?:你(?:还)?记得|还记得|你知道|你叫什么|你的名字是什么)"
    r"|(?:叫什么|是什么|是啥|记得吗)\s*[?？]?\s*$"
)

# 风格类信号：怎么说话（长度/格式/语气）
STYLE_PATTERN = re.compile(
    r"简洁|简短|长篇|啰嗦|别用|不要用|格式|语气|口吻|emoji|表情|列表|加粗|Markdown|markdown"
)

LESSON_KINDS = ("identity", "style", "fact")

# identity 类不占普通配额，但也要有上限——否则反复改名会让这段无界膨胀
IDENTITY_MAX = 5


def detect_correction(text: str) -> bool:
    """用户消息是否含纠正信号。"""
    return any(p in text for p in CORRECTION_PATTERNS)


def is_question(text: str) -> bool:
    """是否是疑问句（用于区分"问身份"与"定身份"）。"""
    return bool(QUESTION_PATTERN.search((text or "").strip()))


def classify_lesson(content: str) -> str:
    """教训分类（纯正则，零 LLM）：identity / style / fact。

    identity 最优先——身份设定是人格锚点，必须永久注入。
    但只有**陈述**才算设定：疑问句是在问身份，不是在定身份。
    """
    text = content or ""
    if IDENTITY_PATTERN.search(text) and not is_question(text):
        return "identity"
    if STYLE_PATTERN.search(text):
        return "style"
    return "fact"


def save_lesson(content: str, context: str = "") -> int:
    """存一条教训：用户原话 + 被纠正的 AI 回复上下文（统一脱敏）。

    去重语义：同 content 已存在时不再插新行，只刷新 created_at 与 context
    （重复纠正 = 这条更重要，让它排到更前面），返回既有行 id。
    """
    from app.services.sanitize import sanitize
    content = sanitize(content)[:300]
    context = sanitize(context)[:300]
    kind = classify_lesson(content)
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO lessons (content, context, created_at, kind)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(content) DO UPDATE SET
                 created_at = excluded.created_at,
                 context = excluded.context,
                 kind = excluded.kind""",
            (content, context, datetime.now(timezone.utc).isoformat(), kind),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM lessons WHERE content = ?", (content,)
        ).fetchone()
        return row["id"] if row else 0
    finally:
        conn.close()


def get_lessons_injection(limit: int = 5) -> str:
    """教训注入：identity 类全部在前（不占配额）+ 最近 limit 条其他教训。

    DISTINCT 是兜底（防迁移遗漏的老库仍出现重复行）。
    排序：越新的越靠后，权重感更强（LLM 对末尾更敏感）。
    """
    conn = connect()
    try:
        identity = conn.execute(
            "SELECT DISTINCT content FROM lessons WHERE kind = 'identity' "
            "ORDER BY id DESC LIMIT ?",
            (IDENTITY_MAX,),
        ).fetchall()
        others = conn.execute(
            "SELECT DISTINCT content FROM lessons WHERE kind != 'identity' "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    lines = [f"- {r['content']}" for r in reversed(identity)]
    lines += [f"- {r['content']}" for r in reversed(others)]
    _record_hits([r["content"] for r in identity] + [r["content"] for r in others])
    return "\n".join(lines)


def _record_hits(contents: list[str]) -> None:
    """记一次注入命中（不衰减的累计使用次数）。

    用途是回答"这条教训到底有没有被用上"——没被命中过的大概是噪声
    （实测线上有一条小说剧情正文被误判成纠正，180 字占满注入窗口）。
    失败不影响注入本身。
    """
    if not contents:
        return
    conn = connect()
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            "UPDATE lessons SET hit_count = COALESCE(hit_count, 0) + 1, last_hit_at = ? "
            "WHERE content = ?",
            [(now, c) for c in contents],
        )
        conn.commit()
    except Exception:  # noqa: BLE001 — 统计失败不该让聊天挂掉
        pass
    finally:
        conn.close()


def count_lessons_since(since_iso: str) -> int:
    """统计某时间以来的教训数（周报用）。"""
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM lessons WHERE created_at >= ?", (since_iso,)
        ).fetchone()["c"]
    finally:
        conn.close()
    return n
