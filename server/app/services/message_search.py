"""消息全文搜索（第 6.26 课）：关键词在全部聊天记录里找人话。

设计取舍：
- 用 SQL LIKE 全扫描而不是向量检索——用户目标是"找到我/小月说过的那句话"，
  关键词精确子串匹配 + 时间倒序，结果可预期；向量检索按语义跑偏反而不讨好。
- 多关键词（空格/逗号/顿号分隔）做 AND，缩小结果集。
- 长消息截取第一个命中点附近的片段（前 40 后 80 字），突出上下文。
"""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.models.database import connect

TZ = ZoneInfo("Asia/Shanghai")

SEARCH_PATTERNS = (
    re.compile(r"^(?:搜索|搜|查)(?:一下|查|找)?聊天记录[:：]?\s*(.+)$"),
    re.compile(r"^聊天记录(?:里)?(?:搜索|搜|查|找)[:：]?\s*(.+)$"),
    re.compile(r"^(?:帮我|请)?(?:搜一下|搜索|查一下)(?:历史)?消息[:：]?\s*(.+)$"),
    re.compile(r"^(?:帮我|请)?(?:搜一下|搜索)(?:历史)?聊天记录[:：]?\s*(.+)$"),
)

MAX_HITS = 20
SNIPPET_BEFORE = 40
SNIPPET_AFTER = 80


def parse_search_command(msg: str) -> str | None:
    for pat in SEARCH_PATTERNS:
        m = pat.match(msg.strip())
        if m:
            return m.group(1).strip()
    return None


def _split_terms(keyword: str) -> list[str]:
    return [t for t in re.split(r"[，,、\s]+", keyword.strip()) if t]


def _snippet(content: str, terms: list[str]) -> str:
    """截取第一个命中点附近片段；单条消息超长时两端加省略号。"""
    text = content.replace("\n", " ")
    pos = -1
    lower = text.casefold()
    for t in terms:
        p = lower.find(t.casefold())
        if p >= 0 and (pos < 0 or p < pos):
            pos = p
    if pos < 0:
        head = text[:SNIPPET_BEFORE + SNIPPET_AFTER]
        return head + ("…" if len(text) > len(head) else "")
    start = max(0, pos - SNIPPET_BEFORE)
    end = min(len(text), pos + SNIPPET_AFTER)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end] + suffix


def search_messages(keyword: str, limit: int = MAX_HITS) -> dict:
    """返回 {"query": kw, "total": N, "results": [显示就绪的命中]}。"""
    terms = _split_terms(keyword)
    if not terms:
        return {"query": keyword, "total": 0, "results": []}
    limit = max(1, min(limit, 50))

    where = " AND ".join(["content LIKE ?"] * len(terms))
    params = [f"%{t}%" for t in terms]
    conn = connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM memories WHERE {where}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"SELECT id, sender, content, ts FROM memories WHERE {where} "
            "ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    finally:
        conn.close()

    results = []
    for r in rows:
        ts_local = _db_to_local(r["ts"])
        results.append(
            {
                "id": r["id"],
                "sender": r["sender"],
                "sender_name": "你" if r["sender"] == "user" else "小月",
                "ts_local": ts_local,
                "snippet": _snippet(r["content"], terms),
            }
        )
    return {"query": keyword, "total": total, "results": results}


def _db_to_local(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso[:16]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(TZ).strftime("%m-%d %H:%M")


def format_results(keyword: str) -> str:
    """聊天命令回复格式。"""
    payload = search_messages(keyword)
    hits = payload["results"]
    if not hits:
        return f"🔍 没有在聊天记录里找到「{keyword}」"
    lines = [f"🔍 找到 {payload['total']} 条命中" + (f"（显示前 {len(hits)} 条）：" if payload["total"] > len(hits) else "：")]
    for h in hits:
        lines.append(f"\n[{h['ts_local']}] {h['sender_name']}：{h['snippet']}")
    return "\n".join(lines)
