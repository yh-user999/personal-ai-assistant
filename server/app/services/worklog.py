"""工作日志服务：对话式记录（"记录：下午3点到5点调RAG性能"）→ 结构化入库。

时间范围解析支持：
- "14:00-17:00"、"14:00至16:30"、"9~11"（数字格式，原样保留）
- "下午3点到5点"、"上午9点到11点"、"晚上7点到9点"（中文口语，自动 +12 转 24 小时制）
"""
import re
from datetime import datetime, timezone

from app.models.database import connect

# 数字格式：14:00-17:00 / 14:00至16:30 / 9~11 / 14:00到16:30
_TIME_RANGE_RE = re.compile(
    r"(\d{1,2}(?::\d{2})?)\s*[-\~至到]\s*(\d{1,2}(?::\d{2})?)"
)
# 中文口语：上午9点到11点 / 下午3点到5点 / 晚上7-9点
# 注意：① 数字后可有可无"点"字；② "到"和"至"都要支持（口语说"到"）
_CN_RANGE_RE = re.compile(r"(上午|下午|晚上)?\s*(\d{1,2})\s*点?\s*[-\~至到]\s*(\d{1,2})\s*点")


def _parse_time_range(content: str) -> str:
    # 先试中文口语（特征：结尾带"点"字），再试纯数字格式
    m = _CN_RANGE_RE.search(content)
    if m:
        period, h1, h2 = m.group(1), int(m.group(2)), int(m.group(3))
        # 下午/晚上 +12（如 3点 → 15点）；12点整除外
        if period in ("下午", "晚上"):
            if h1 < 12:
                h1 += 12
            if h2 < 12:
                h2 += 12
        return f"{h1:02d}:00-{h2:02d}:00"
    m = _TIME_RANGE_RE.search(content)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return ""


def add_log(content: str, project: str = "", user_id: str | None = None) -> int:
    """新增一条工作日志。入库前统一脱敏，按用户主体保存。"""
    from app.core.memory import normalize_user_id
    from app.services.sanitize import sanitize

    uid = normalize_user_id(user_id)
    content = sanitize(content)
    project = sanitize(project)
    now = datetime.now(timezone.utc)
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO work_log (user_id, date, time_range, content, project, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uid, now.date().isoformat(), _parse_time_range(content), content, project, now.isoformat()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()
