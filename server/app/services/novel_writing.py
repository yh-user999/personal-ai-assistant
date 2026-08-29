"""小说写作增强（第 6.25 课）：设定冲突检查 + 续写辅助 + 写作台账。

- 设定冲突检查：新写内容 vs 权威设定（小说设定卡 + 已确认 facts），
  flash LLM 一次调用逐条比对，输出"你写的 ↔ 已有设定 ↔ 依据 ↔ 建议"。
- 续写辅助：注入设定卡 + 剧情背景（知识库检索 + 邻域扩展）+ 写作偏好，
  按参考小说文风续写 300~500 字。
- 写作台账：零 LLM 规则，`写作记录：第X章 N字` 入库，`写作进度` 汇总。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.core import knowledge, llm, memory
from app.models.database import connect

TZ = ZoneInfo("Asia/Shanghai")

CONFLICT_PATTERNS = (
    re.compile(r"^(?:帮我|请)?(?:检查|查一下|查查)(?:小说)?设定(?:冲突|矛盾)[:：]?\s*(.+)$"),
    re.compile(r"^设定(?:冲突|矛盾)检查[:：]?\s*(.+)$"),
)
CONTINUE_RE = re.compile(r"^(?:帮我|请)?(?:续写|接着写)(?:一下|一段)?[:：]?\s*(.+)$")
LOG_RE = re.compile(
    r"^写作记录[:：]?\s*(?:第\s*(\d+)\s*章)?\s*(?:写了|码了|共)?(\d+)\s*字$"
)

_PATH_HINT_RE = re.compile(r"[A-Za-z]:[\\/]|[\\/]|盘|\.(?:txt|md|doc|docx)$")


def looks_like_file_path(s: str) -> bool:
    return bool(_PATH_HINT_RE.search(s.strip()))


# ── ① 设定冲突检查 ─────────────────────────────────────────

def parse_conflict_command(msg: str) -> str | None:
    for pat in CONFLICT_PATTERNS:
        m = pat.match(msg.strip())
        if m:
            return m.group(1).strip()
    return None


def parse_conflicts_json(text: str) -> list[dict]:
    """容错解析 LLM 输出的冲突数组（容忍前后缀噪声）。"""
    m = re.search(r"\[[\s\S]*\]", text)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data[:12]:
        if not isinstance(item, dict):
            continue
        problem = str(item.get("problem", "")).strip()
        if not problem:
            continue
        out.append(
            {
                "quote": str(item.get("quote", "")).strip()[:120],
                "problem": problem[:200],
                "setting": str(item.get("setting", "")).strip()[:200],
                "basis": str(item.get("basis", "")).strip()[:80],
                "suggestion": str(item.get("suggestion", "")).strip()[:200],
            }
        )
    return out


def _build_authority(novel_facts: list[str], facts_text: str) -> tuple[str, int]:
    """权威设定块拼装（设定冲突检查与续写共用，两处文案曾漂移）。

    返回 (authority 文本, 已纳入设定条数)。
    """
    card_head = "【小说设定卡】"
    facts_head = "【已确认设定（facts 永久层）】"
    parts = []
    count = 0
    if novel_facts:
        parts.append(card_head + "\n- " + "\n- ".join(novel_facts))
        count += len(novel_facts)
    if facts_text and facts_text != "（暂无）":
        parts.append(facts_head + "\n" + facts_text)
        count += facts_text.count("\n") + 1
    authority = "\n\n".join(parts) if parts else "（暂无已确认设定）"
    return authority, count


async def check_conflicts(text: str) -> dict:
    """返回 {"reply": str}（已格式化，可直接当聊天回复）。"""
    novel_facts = knowledge.get_novel_facts(text)
    facts_text = memory.get_facts_injection()
    authority, checked_count = _build_authority(novel_facts, facts_text)

    system = (
        "你是资深小说设定审校员。用户提供【权威设定】（已确认的小说设定，不可违背）"
        "和【新写内容】。任务：找出【新写内容】中与【权威设定】冲突之处。\n"
        "规则：只报确有冲突的条目；疑似冲突、风格差异、新增未确认设定不报；没有冲突输出空数组。\n"
        '严格输出 JSON 数组，每项字段：quote（新写内容中的原句）、problem（冲突描述）、'
        'setting（与之冲突的已有设定）、basis（依据出处）、suggestion（修改建议）。'
    )
    user = f"【权威设定】\n{authority}\n\n【新写内容】\n{text[:4000]}"
    try:
        out = await llm.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.2,
            max_tokens=1500,
        )
    except Exception as e:
        return {"reply": f"😅 冲突检查暂时没跑通（LLM 调用失败：{type(e).__name__}），稍后再试"}

    conflicts = parse_conflicts_json(out)
    if not conflicts:
        return {
            "reply": f"✅ 未发现与已确认设定的冲突（对照了 {checked_count} 条权威设定）。\n"
                     f"提醒：新引入但尚未确认的设定不在检查范围，确认后可让我「记住」它。"
        }
    lines = [f"⚠️ 发现 {len(conflicts)} 处设定冲突："]
    for i, c in enumerate(conflicts, 1):
        lines.append(f"\n{i}. {c['problem']}")
        if c["quote"]:
            lines.append(f"   你写的：{c['quote']}")
        if c["setting"]:
            lines.append(f"   已有设定：{c['setting']}")
        if c["basis"]:
            lines.append(f"   依据：{c['basis']}")
        if c["suggestion"]:
            lines.append(f"   建议：{c['suggestion']}")
    return {"reply": "\n".join(lines)}


# ── ② 续写辅助 ─────────────────────────────────────────────

def parse_continue_command(msg: str) -> str | None:
    m = CONTINUE_RE.match(msg.strip())
    if m:
        return m.group(1).strip()
    return None


async def continue_story(text: str) -> str:
    """注入设定 + 剧情背景，续写 300~500 字。失败给友好提示。"""
    novel_facts = knowledge.get_novel_facts(text)
    hits = await knowledge.search_knowledge(text, top_k=3)
    hits = knowledge.expand_chunks(hits, radius=1, max_chars=2000)
    background = knowledge.format_knowledge_injection(hits) or "（未检索到相关剧情背景）"
    facts_text = memory.get_facts_injection()

    authority, _ = _build_authority(novel_facts, facts_text)

    system = (
        "你是网络小说写手，文风参考《寂静杀戮》：冷静克制、短句有力、动作感强、"
        "心理描写克制。根据【权威设定】和【剧情背景】，从【当前段落】之后自然续写 300~500 字。\n"
        "规则：不得引入与权威设定冲突的新设定；承接上一句的视角与节奏；"
        "结尾停在有悬念或情绪落点的句子。只输出正文，不要任何解释或标题。"
    )
    user = f"【权威设定】\n{authority}\n\n【剧情背景】\n{background}\n\n【当前段落】\n{text[:3000]}"
    try:
        out = await llm.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.8,
            max_tokens=1200,
        )
    except Exception as e:
        return f"😅 续写暂时没跑通（LLM 调用失败：{type(e).__name__}），稍后再试"
    return out.strip() or "（模型没吐出内容，重试一次看看）"


# ── ③ 写作台账（零 LLM）────────────────────────────────────

def parse_writing_log(msg: str) -> tuple[str | None, int] | None:
    """`写作记录：第10章 3200字` → (chapter, words)。"""
    m = LOG_RE.match(msg.strip())
    if not m:
        return None
    words = int(m.group(2))
    if words <= 0 or words > 100000:
        return None
    return (m.group(1), words)


def add_writing_log(chapter: str | None, words: int) -> int:
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO writing_log (chapter, words, created_at) VALUES (?, ?, ?)",
            (chapter, words, datetime.now(TZ).astimezone(ZoneInfo("UTC")).isoformat()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def writing_summary() -> str:
    """`写作进度` 汇总：累计/近7天/今日 + 连续天数 + 最近记录。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT chapter, words, created_at FROM writing_log ORDER BY id DESC LIMIT 100"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return "📝 还没有写作记录。用「写作记录：第X章 N字」开始记账吧。"

    today = datetime.now(TZ).date()
    total = 0
    week = 0
    day = 0
    days: set[str] = set()
    chapter_nums = [int(r["chapter"]) for r in rows if r["chapter"] and r["chapter"].isdigit()]
    latest_chapter = max(chapter_nums) if chapter_nums else None
    for r in rows:
        dt = datetime.fromisoformat(r["created_at"]).astimezone(TZ)
        d = dt.date()
        total += r["words"]
        if (today - d).days < 7:
            week += r["words"]
        if d == today:
            day += r["words"]
        days.add(d.isoformat())

    # 连续写作天数：从今天往回数
    streak = 0
    cursor = today
    if today.isoformat() in days:
        while cursor.isoformat() in days:
            streak += 1
            cursor -= timedelta(days=1)
    else:
        # 今天还没写：从昨天往回数
        cursor = today - timedelta(days=1)
        while cursor.isoformat() in days:
            streak += 1
            cursor -= timedelta(days=1)

    def _fmt(r) -> str:
        dt = datetime.fromisoformat(r["created_at"]).astimezone(TZ).strftime("%m-%d")
        ch = f"第{r['chapter']}章 " if r["chapter"] else ""
        return f"  {dt} {ch}{r['words']} 字"

    recent_lines = "\n".join(_fmt(r) for r in rows[:5])
    lines = [
        f"📝 写作台账：",
        f"  累计：{total:,} 字（{len(rows)} 次记录）",
        f"  近 7 天：{week:,} 字 ｜ 今日：{day:,} 字",
        f"  连续写作：{streak} 天",
        f"  最新章节：第{latest_chapter}章" if latest_chapter else "  最新章节：未标注",
        f"  最近记录：",
        recent_lines,
    ]
    return "\n".join(lines)


# 兼容别名（chat.py 曾直接调私有名；统一走公开名）
_looks_like_file_path = looks_like_file_path
