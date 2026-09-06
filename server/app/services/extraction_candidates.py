"""低置信实体候选账本 + 自动抽取预算闸（自 index_healer.py 迁出，逻辑等价）。

独立职责：
- 每日抽取预算（auto_extract_log 表：同词同日幂等 + 每日上限）
- 候选池（entity_candidates 表：低置信抽取 → 主人确认/废弃）
- auto_extract_task：heal 兜底成功后的后台自动抽取编排
调用方兼容：index_healer 以再导出保持旧引用路径。
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone

from openai import OpenAIError

from app.models.database import connect

AUTO_DAILY_LIMIT = 3   # 预算闸：每天最多自动抽取 3 次
AUTO_MAX_BLOCKS = 10   # 成本闸：自动抽取最多读 10 个候选块（手动模式 40）
AUTO_MIN_EVIDENCE = 2  # 置信闸：名字在 ≥2 块出现才直接入库，1 块进候选池


def _reserve_extract_slot(kind_word: str) -> bool:
    """原子占位：同词同日唯一（幂等闸）+ 每日 ≤AUTO_DAILY_LIMIT（预算闸）。

    先 INSERT 占位再数当日行数——两个并发任务同时检查时，INSERT OR IGNORE
    的 day_key 唯一性保证只有一个拿到名额（实测过并发双触发都通过的竞态）。
    返回 True=获得名额，False=被闸拦截。
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_key = f"{today}:{kind_word}"
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO auto_extract_log "
            "(kind_word, book, extracted_at, names_count, day_key) "
            "VALUES (?, '', ?, 0, ?)",
            (kind_word, datetime.now(timezone.utc).isoformat(), day_key),
        )
        if cur.rowcount == 0:
            return False  # 同词同日已占位（幂等）
        n_today = conn.execute(
            "SELECT COUNT(*) AS c FROM auto_extract_log WHERE day_key LIKE ?",
            (today + ":%",),
        ).fetchone()["c"]
        if n_today > AUTO_DAILY_LIMIT:
            conn.execute("DELETE FROM auto_extract_log WHERE day_key=?", (day_key,))
            conn.commit()
            return False  # 超每日限额，释放占位
        conn.commit()
        return True
    finally:
        conn.close()


def _settle_extract_slot(kind_word: str, book: str, names_count: int) -> None:
    """抽取结束后回填占位行（book 与入库数）。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_key = f"{today}:{kind_word}"
    conn = connect()
    try:
        conn.execute(
            "UPDATE auto_extract_log SET book=?, names_count=? WHERE day_key=?",
            (book, names_count, day_key),
        )
        conn.commit()
    finally:
        conn.close()


def _chunk_evidence(book: str, name: str) -> int:
    """名字在书中出现的块数（零 LLM 的置信依据）。"""
    conn = connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM knowledge_chunks WHERE doc_name=? AND content LIKE ?",
            (book, f"%{name}%"),
        ).fetchone()["c"]
    finally:
        conn.close()


async def auto_extract_task(
    words: list[str],
    book: str,
    user_id: str | None = None,
    request_id: str | None = None,
) -> dict | None:
    """后台自动抽取入口（chat heal 成功后触发，fire-and-forget）。"""
    if not words or not book:
        return None
    from app.core.memory import normalize_user_id
    from app.services.llm_usage import logical_request_id

    uid = normalize_user_id(user_id)
    base_request_id = request_id or logical_request_id(
        "index_healer_auto_extract", uid, f"{book}:{words[0]}"
    )
    kind_word = words[0]
    if not _reserve_extract_slot(kind_word):
        return {"kind": kind_word, "skipped": "budget_or_duplicate"}

    from app.services import novel_entities

    try:
        extractor = novel_entities.extract_entities
        try:
            parameters = inspect.signature(extractor).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        extractor_kwargs = {"dry_run": True, "max_blocks": AUTO_MAX_BLOCKS}
        if "user_id" in parameters or accepts_kwargs:
            extractor_kwargs["user_id"] = uid
        if "request_id" in parameters or accepts_kwargs:
            extractor_kwargs["request_id"] = base_request_id
        payload = await extractor(book, kind_word, **extractor_kwargs)
    except (OpenAIError, TimeoutError, RuntimeError, ValueError, TypeError) as e:
        _settle_extract_slot(kind_word, book, 0)
        return {"kind": kind_word, "skipped": f"extract_error: {e}"}

    names = payload.get("names") or []
    confirmed: list[dict] = []
    for item in names:
        name = item.get("name")
        if not name:
            continue
        evidence = _chunk_evidence(book, name)
        if evidence >= AUTO_MIN_EVIDENCE:
            confirmed.append({"name": name, "first_chunk": item.get("first_chunk")})
        else:
            candidate_add(book, kind_word, name, item.get("first_chunk"))

    if confirmed:
        novel_entities.confirm_extracted(
            {
                "book": book,
                "kind": kind_word,
                "names": confirmed,
                "group_name": payload.get("group_name") or "",
                "group_size": payload.get("group_size") or 0,
            }
        )
    _settle_extract_slot(kind_word, book, len(confirmed))
    return {
        "kind": kind_word,
        "book": book,
        "confirmed": len(confirmed),
        "candidates": len(names) - len(confirmed),
    }


# ── 候选池（低置信抽取）────────────────────────────────────

def candidate_add(book: str, kind: str, name: str, first_chunk) -> int:
    conn = connect()
    try:
        cur = conn.execute(
            """INSERT INTO entity_candidates (book, kind, name, first_chunk, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(book, kind, name) DO NOTHING""",
            (book, kind, name, first_chunk, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def candidate_list(limit: int = 10) -> list[dict]:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM entity_candidates WHERE status='pending' ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()]
    finally:
        conn.close()


def candidate_confirm(name: str) -> int:
    """候选转正：写进实体索引（verified=1）并标记候选状态。

    注意分段取连：upsert_entity 内部会复用并关闭线程缓存连接，
    若外层还握着同一连接继续用会 ProgrammingError（实测踩过）。
    """
    from app.services.novel_entities import upsert_entity

    conn = connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM entity_candidates WHERE name=? AND status='pending'", (name,)
        ).fetchall()]
    finally:
        conn.close()
    if not rows:
        return 0
    for r in rows:
        upsert_entity(r["book"], r["name"], r["kind"],
                      first_chunk=r["first_chunk"], verified=1)
    conn = connect()
    try:
        for r in rows:
            conn.execute(
                "UPDATE entity_candidates SET status='confirmed' WHERE id=?", (r["id"],)
            )
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def candidate_discard(name: str) -> int:
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE entity_candidates SET status='discarded' WHERE name=? AND status='pending'",
            (name,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
