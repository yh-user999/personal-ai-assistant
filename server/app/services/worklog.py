"""工作日志服务：对话式记录（"记录：下午2-5点调RAG性能"）→ 结构化入库。"""
import re
from datetime import datetime, timezone

from app.models.database import connect

# 简单解析：尝试提取时间范围，如 "14:00-17:00" / "下午2-5点"
_TIME_RANGE_RE = re.compile(
    r"(\d{1,2}(?::\d{2})?)\s*[-~至]\s*(\d{1,2}(?::\d{2})?)"
)
_CN_RANGE_RE = re.compile(r"(?:下午|晚上)?\s*(\d{1,2})\s*[-~至]\s*(\d{1,2})\s*点")


def _parse_time_range(content: str) -> str:
    m = _TIME_RANGE_RE.search(content)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = _CN_RANGE_RE.search(content)
    if m:
        return f"{int(m.group(1)):02d}:00-{int(m.group(2)):02d}:00"
    return ""


def add_log(content: str, project: str = "") -> int:
    """新增一条工作日志。content 为去掉"记录："前缀后的文本。"""
    now = datetime.now(timezone.utc)
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO work_log (date, time_range, content, project, created_at) VALUES (?, ?, ?, ?, ?)",
            (now.date().isoformat(), _parse_time_range(content), content, project, now.isoformat()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()
