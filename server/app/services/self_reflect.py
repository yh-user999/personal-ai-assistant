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
    "起名", "名字叫", "就叫你", "你的名字", "叫你",
)

# 身份设定类信号：给她起名、定身份、定自称——永久最高优先级
IDENTITY_PATTERN = re.compile(
    r"起名|名字叫|就叫你|你的名字|叫你|你就叫|自称|你是我的|以后你(?:就)?是"
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


def classify_lesson(content: str) -> str:
    """教训分类（纯正则，零 LLM）：identity / style / fact。

    identity 最优先——身份设定是人格锚点，必须永久注入。
    """
    text = content or ""
    if IDENTITY_PATTERN.search(text):
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
    return "\n".join(lines)


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
