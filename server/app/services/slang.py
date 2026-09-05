"""黑话模块（一至三期合一实现）。

一期：显式教学（记黑话：X = Y）+ 追溯纠正（不对，X 是 Y 的意思）+ 词命中注入
     （带出处）+ 共享只读（主人 shared 对访客可见）+ 管理命令。
二期：语境推断（链接 + 短句 → 后台 LLM 候选）+ 转正状态机（candidate 被使用
     ≥2 次且未纠正 → confirmed）+ 语义兜底（问"X 是啥意思"时按词面/反查命中）。
三期：淘汰策略（candidate ≥180 天未用删除；低使用 confirmed ≥365 天降级）。

设计稿：docs/黑话模块实施方案.md（用户拍板：黑话是共享的）。
"""
import logging
import re
from datetime import datetime, timedelta, timezone

from openai import OpenAIError

from app.core import llm
from app.core.memory import normalize_user_id, owner_user_id
from app.models.database import connect
from app.services.sanitize import sanitize

logger = logging.getLogger("assistant.slang")

# ── 检测模式 ───────────────────────────────────────────────

_TEACH_RES = (
    re.compile(r"^(?:记|记住)(?:个|一条)?黑话[：:]\s*([\u4e00-\u9fffA-Za-z0-9]{1,12})\s*=\s*(.+)$"),
    re.compile(r"^黑话[「『]([^」』]{1,12})[」』]\s*(?:指|是|的意思?是)\s*(.+)$"),
)
_CORRECT_RES = (
    re.compile(r"^(?:不对|不是)[，,：:\s]*(.{1,12}?)(?:是指|是|指)(.+?)(?:的意思)?[。！!？?]*$"),
    re.compile(r"^刚才(?:说)?的(.{1,12}?)(?:是指|是|指)(.+?)(?:的意思)?[。！!？?]*$"),
)
_MEANING_QUESTION = re.compile(r"是啥意思|什么意思|是什么意思|黑话(?:是)?什么")
_URL_RE = re.compile(r"https?://\S+")
LINK_FOLLOWUP_MAX_CHARS = 15  # 链接后的短句才可能是在起黑话代称
PROMOTE_USE_COUNT = 2          # candidate 被使用 ≥2 次转正
CANDIDATE_TTL_DAYS = 180       # candidate 未用 180 天删除
CONFIRMED_DEMOTE_DAYS = 365    # 低使用 confirmed 365 天未用降级


def parse_teach(msg: str) -> tuple[str, str] | None:
    """显式教学：「记黑话：鸡蛋 = 链接里的免费token福利」。"""
    for pat in _TEACH_RES:
        m = pat.match((msg or "").strip())
        if m:
            term, meaning = m.group(1).strip(), m.group(2).strip()
            if term and meaning:
                return term, meaning
    return None


def parse_correct(msg: str) -> tuple[str, str] | None:
    """追溯纠正：「不对，鸡蛋是免费token的意思」。"""
    for pat in _CORRECT_RES:
        m = pat.match((msg or "").strip())
        if m:
            term, meaning = m.group(1).strip(), m.group(2).strip()
            if term and meaning:
                return term, meaning
    return None


def is_meaning_question(msg: str) -> bool:
    return bool(_MEANING_QUESTION.search(msg or ""))


def detect_link_followup(prev_msg: str, cur_msg: str) -> bool:
    """语境推断触发信号：上一条有链接，本条是 ≤15 字的短句且不含链接。"""
    if not prev_msg or not cur_msg:
        return False
    if not _URL_RE.search(prev_msg):
        return False
    cur = (cur_msg or "").strip()
    return bool(cur) and len(cur) <= LINK_FOLLOWUP_MAX_CHARS and not _URL_RE.search(cur)


# ── 存储 ───────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_term(term: str, meaning: str, user_id: str | None = None, *,
              scope: str = "", status: str = "confirmed",
              source_episode: str = "", context_hint: str = "") -> int:
    """存/更新黑话（按 user+term upsert）。scope 空时按身份默认：
    主人 shared、访客 private。入库前统一脱敏。"""
    uid = normalize_user_id(user_id)
    if not scope:
        scope = "shared" if uid == owner_user_id() else "private"
    term = sanitize(term)[:12]
    meaning = sanitize(meaning)[:300]
    source_episode = sanitize(source_episode)[:200]
    now = _now()
    conn = connect()
    try:
        cur = conn.execute(
            """INSERT INTO slang_terms
               (user_id, term, meaning, context_hint, source_episode, scope, status,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, term, context_hint) DO UPDATE SET
                 meaning = excluded.meaning,
                 source_episode = excluded.source_episode,
                 scope = excluded.scope,
                 status = excluded.status,
                 updated_at = excluded.updated_at""",
            (uid, term, meaning, context_hint, source_episode, scope, status, now, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_slang_injection(message: str, user_id: str | None = None, limit: int = 3) -> str:
    """词命中注入：本人全部条目 + 主人 shared 条目（访客视角）。

    命中时 use_count+1（candidate 使用 ≥2 次自动转正——转正状态机）。
    排序：confirmed 优先，use_count 次之，最近使用兜底。
    """
    uid = normalize_user_id(user_id)
    msg = message or ""
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT * FROM slang_terms
               WHERE (user_id = ? OR (scope = 'shared' AND user_id = ?))
               ORDER BY (status='confirmed') DESC, use_count DESC, last_used_at DESC""",
            (uid, owner_user_id()),
        ).fetchall()
    finally:
        conn.close()
    hits = [dict(r) for r in rows
            if len(r["term"]) >= 2 and r["term"] in msg]
    if not hits and is_meaning_question(msg):
        # 语义兜底：问"X 是啥意思"但词面没命中 → 反查（词出现在 meaning 里）
        core = re.sub(r"是啥意思|什么意思|是什么意思|黑话是什么|吗|啊|呢|[？?！!。\s]+", "", msg)
        hits = [dict(r) for r in rows if 2 <= len(r["term"]) <= 8 and r["term"] in core]
    if not hits:
        return ""
    selected = hits[:limit]
    now = _now()
    conn = connect()
    try:
        for h in selected:
            new_count = h["use_count"] + 1
            new_status = h["status"]
            if h["status"] == "candidate" and new_count >= PROMOTE_USE_COUNT:
                new_status = "confirmed"  # 转正状态机：用两次没被纠正 = 默认对了
                logger.info("[slang] 「%s」候选转正（使用 %d 次）", h["term"], new_count)
            conn.execute(
                "UPDATE slang_terms SET use_count=?, last_used_at=?, status=?, updated_at=? WHERE id=?",
                (new_count, now, new_status, now, h["id"]),
            )
        conn.commit()
    finally:
        conn.close()
    parts = []
    for h in selected:
        head = f"<黑话> 「{h['term']}」＝{h['meaning']}"
        if h["source_episode"]:
            head += f"（出处：{h['source_episode'][:60]}）"
        parts.append(head)
    return "\n".join(parts)


# ── 语境推断（二期，后台任务）──────────────────────────────

INFER_PROMPT = """用户上一条消息包含链接，随后发了一条短句。判断这是否是在给链接/链接里的东西起"黑话代称"（像老人免费领鸡蛋一样，用日常词指代福利/资源/事件）。
若是，输出 {{"is_slang": true, "term": "代称词", "meaning": "这个词指什么"}}；若不是输出 {{"is_slang": false}}。只输出 JSON。

链接消息：{prev}
短句：{cur}
"""


async def infer_candidate(prev_msg: str, cur_msg: str, user_id: str | None = None) -> bool:
    """语境推断：链接+短句 → LLM 判断黑话代称 → 存 candidate。

    失败/非黑话静默返回 False；绝不打扰用户。scope 按身份默认（主人 shared）。
    """
    try:
        result = await llm.chat_json(
            "你是黑话识别助手，只输出 JSON。",
            INFER_PROMPT.replace("{prev}", (prev_msg or "")[:300])
                       .replace("{cur}", cur_msg),
        )
    except OpenAIError as e:
        logger.warning("[slang] 语境推断 LLM 失败: %s", e)
        return False
    if not result.get("is_slang"):
        return False
    term = str(result.get("term") or "").strip()
    meaning = str(result.get("meaning") or "").strip()
    if not (2 <= len(term) <= 12) or not meaning:
        return False
    episode = f"链接 +「{cur_msg.strip()}」"
    save_term(term, meaning, user_id=user_id, status="candidate",
              source_episode=episode)
    logger.info("[slang] 语境推断候选:「%s」=%s", term, meaning[:40])
    return True


# ── 管理命令（三期）────────────────────────────────────────

def list_terms(user_id: str | None = None) -> list[dict]:
    uid = normalize_user_id(user_id)
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT * FROM slang_terms
               WHERE user_id = ? OR (scope='shared' AND user_id = ?)
               ORDER BY status, use_count DESC""",
            (uid, owner_user_id()),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _own_term(term: str, user_id: str | None = None) -> dict | None:
    uid = normalize_user_id(user_id)
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM slang_terms WHERE user_id=? AND term=? AND context_hint=''",
            (uid, term),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_scope(term: str, scope: str, user_id: str | None = None) -> int:
    """主人专属：shared / private 切换。"""
    uid = normalize_user_id(user_id)
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE slang_terms SET scope=?, updated_at=? WHERE user_id=? AND term=? AND context_hint=''",
            (scope, _now(), uid, term),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def delete_term(term: str, user_id: str | None = None) -> int:
    uid = normalize_user_id(user_id)
    conn = connect()
    try:
        cur = conn.execute(
            "DELETE FROM slang_terms WHERE user_id=? AND term=? AND context_hint=''",
            (uid, term),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def evict_stale_slang() -> dict:
    """淘汰策略（挂 evict_stale）：candidate ≥180 天未用删除；
    低使用 confirmed ≥365 天未用降级为 candidate。"""
    conn = connect()
    try:
        c_cutoff = (datetime.now(timezone.utc) - timedelta(days=CANDIDATE_TTL_DAYS)).isoformat()
        n_del = conn.execute(
            "DELETE FROM slang_terms WHERE status='candidate' "
            "AND (last_used_at IS NULL OR last_used_at < ?) AND created_at < ?",
            (c_cutoff, c_cutoff),
        ).rowcount
        d_cutoff = (datetime.now(timezone.utc) - timedelta(days=CONFIRMED_DEMOTE_DAYS)).isoformat()
        n_demote = conn.execute(
            "UPDATE slang_terms SET status='candidate', updated_at=? "
            "WHERE status='confirmed' AND use_count <= 1 "
            "AND (last_used_at IS NULL OR last_used_at < ?)",
            (_now(), d_cutoff),
        ).rowcount
        conn.commit()
        return {"deleted_candidates": n_del, "demoted_confirmed": n_demote}
    finally:
        conn.close()
