"""小说写作增强二期：章节分析 + 跨章剧情存档。

- 章节分析：`分析章节：<正文>` → 残留检测（零 LLM 预检）+ 逻辑/设定/称谓/
  节奏问题清单（1 次 LLM，JSON 容错解析）；识别到章节号自动落档 chapter_notes。
- 章节存档：`章节存档：第X章 <摘要>（伏笔：<...>）` 零 LLM 入库。
- 前情提要：`build_continuity_block()` 读最近章节摘要 + 未回收伏笔，
  供续写/章节生成路径注入，保证写第 N 章时知道第 N-1 章写了什么。
- 被动抓取：`capture_chapter_reply` 由生成档长回复后台调用，闪存 LLM
  提炼摘要入库（解决"用户从不打命令"——写作台账/goals 同款教训）。

冲突检查对照的 facts 三元组里"少爷/地方豪强"没有姓氏锚点，称谓漂移
（孙家/赵家混用）抓不住——本模块把正文本身交 LLM 对照权威设定逐条审。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.core import knowledge, llm, memory
from app.models.database import connect
from app.services import sepia
from app.services.novel_writing import _build_authority, looks_like_file_path

TZ = ZoneInfo("Asia/Shanghai")

# 命令解析：冒号必须有（防误吞普通聊天），前缀"帮我/请"可组合（请帮我/帮我请）
ANALYSIS_RE = re.compile(r"^(?:(?:帮我|请)\s*){0,2}(?:分析章节|章节分析|章节合理性|逻辑分析)[:：]\s*(.+)$")
ARCHIVE_RE = re.compile(r"^章节存档[:：]\s*第\s*([0-9一二三四五六七八九十百两]+)\s*章\s*(.+)$")

# 正文头部的章节号：'第一章' / '第12章'（允许标题行前有少量空白）
_CH_HEAD_RE = re.compile(r"^\s*第\s*([0-9一二三四五六七八九十百两]+)\s*[章回]")

_CN_NUM = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "百": 100,
}

# 兼容旧调用方的正则导出；实际判据统一由 Sepia 规则模块维护。
_ENDING_RE = sepia.SURFACE_RULES["章节尾标记"]
_META_RE = sepia.SURFACE_RULES["AI元话语"]

# 续写/章节生成路径的 prompt 常量
WORD_TARGET_NOTE = "每章只承载一个主要转折；按网文标准每章约3000字"


# ── 章节号 ─────────────────────────────────────────────────

def cn_to_int(s: str) -> int | None:
    """中文数字 → int（支持十/十二/二十/一百零五/二百三十等常见形态）。"""
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        value = int(s)
        return value if value > 0 else None

    total = 0
    number = 0
    for ch in s:
        value = _CN_NUM.get(ch)
        if value is None:
            return None
        if ch in ("十", "百"):
            total += (number or 1) * value
            number = 0
        else:
            number = value
    result = total + number
    return result if result > 0 else None


def extract_chapter_no(text: str) -> str | None:
    """从正文头部识别 `第一章/第12章` → 阿拉伯数字字符串；无章节号返回 None。"""
    m = _CH_HEAD_RE.match(text.lstrip("\ufeff \u3000"))
    if not m:
        return None
    n = cn_to_int(m.group(1))
    return str(n) if n and n > 0 else None


# ── 残留检测（零 LLM）──────────────────────────────────────

def detect_residue(text: str) -> list[tuple[str, str]]:
    """返回统一的表层预检结果，兼容旧的章节尾/AI 元话语类型。"""
    return sepia.detect_surface_violations(text)


def _word_target_note(user_id: str | None = None) -> str:
    """从当前用户 facts 找"每章约3000字"类目标；找不到返回空串。"""
    uid = memory.normalize_user_id(user_id)
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT subject, predicate, object FROM facts WHERE user_id=? AND "
            "(predicate LIKE '%字数%' OR object LIKE '%每章%字%')",
            (uid,),
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        m = re.search(r"(\d{3,5})\s*字", r["object"])
        if m:
            return r["object"].strip()[:60]
    return ""


def _word_count_block(text: str, user_id: str | None = None) -> list[str]:
    """字数统计 vs 当前用户 facts 目标对照（目标缺失时只报实测字数）。"""
    words = len(re.sub(r"\s", "", text))
    lines = [f"字数：约 {words} 字"]
    target_note = _word_target_note(user_id)
    if target_note:
        m = re.search(r"(\d{3,5})\s*字", target_note)
        if m:
            target = int(m.group(1))
            deviation = words - target
            if abs(deviation) > target * 0.3:
                lines.append(
                    f"字数偏差：目标约 {target} 字（{target_note}），"
                    f"本章 {'超出' if deviation > 0 else '不足'} 约 {abs(deviation)} 字，"
                    "超载常见于事件塞太多——对照下方节奏评估看是否要拆章"
                )
    return lines


# ── 存档（chapter_notes）───────────────────────────────────

def _now_iso() -> str:
    return datetime.now(TZ).astimezone(timezone.utc).isoformat()


def upsert_chapter_note(
    chapter: str,
    summary: str,
    threads: list[str] | None = None,
    source: str = "manual",
) -> None:
    """按 chapter 幂等 upsert；threads 转 JSON 字符串。"""
    threads_json = json.dumps(
        [str(t).strip()[:100] for t in (threads or []) if str(t).strip()][:10],
        ensure_ascii=False,
    )
    now = _now_iso()
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO chapter_notes (chapter, summary, threads, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chapter) DO UPDATE SET
              summary=excluded.summary, threads=excluded.threads,
              source=excluded.source, updated_at=excluded.updated_at
            """,
            (str(chapter), summary.strip()[:500], threads_json, source, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def get_chapter_note(chapter: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT chapter, summary, threads, source FROM chapter_notes WHERE chapter=?",
            (str(chapter),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "chapter": row["chapter"],
        "summary": row["summary"],
        "threads": _loads_threads(row["threads"]),
        "source": row["source"],
    }


def _loads_threads(raw: str) -> list[str]:
    try:
        data = json.loads(raw or "[]")
        return [str(t) for t in data] if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def parse_archive_command(msg: str) -> tuple[str, str, list[str]] | None:
    """`章节存档：第X章 <摘要>（伏笔：<...>）` → (chapter, summary, threads)。"""
    m = ARCHIVE_RE.match(msg.strip())
    if not m:
        return None
    n = cn_to_int(m.group(1))
    if not n or n <= 0:
        return None
    rest = m.group(2).strip()
    threads: list[str] = []
    fm = re.search(r"[（(]伏笔[:：]\s*(.+?)[)）]\s*$", rest)
    if fm:
        threads = [t.strip() for t in re.split(r"[;；,，、]", fm.group(1)) if t.strip()]
        rest = rest[: fm.start()].strip()
    if not rest:
        return None
    return (str(n), rest[:500], threads)


def build_continuity_block() -> str:
    """前情提要块：最近 8 章摘要 + 全部未回收伏笔。

    写第 N 章前注入，机器人就知道第 N-1 章写了什么。表空返回 ""（不出现）。
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT chapter, summary, threads FROM chapter_notes "
            "ORDER BY CAST(chapter AS INTEGER) DESC LIMIT 8"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return ""
    summaries = [
        f"第{r['chapter']}章：{r['summary']}" for r in reversed(rows)
    ]
    threads: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for t in _loads_threads(r["threads"]):
            if t not in seen:
                seen.add(t)
                threads.append(t)
    parts = ["【前情提要（权威剧情事实，新写内容不得与之矛盾）】"]
    parts.extend(f"- {s}" for s in summaries)
    if threads:
        parts.append("【未回收伏笔（后续章节应自然回收）】")
        parts.extend(f"- {t}" for t in threads)
    block = "\n".join(parts)
    if len(block) > 1500:
        block = block[:1497] + "…"
    return block


# ── 被动抓取（生成档回复 → 自动存档）───────────────────────

def _parse_analysis_json(text: str) -> dict:
    """容错解析 LLM 输出的分析 JSON（容忍前后缀噪声）。"""
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


async def capture_chapter_reply(chapter_no: str, reply: str, user_id: str | None = None) -> None:
    """生成档长回复含"第X章"时的后台自动提炼：闪存 LLM 一次，失败静默。

    无需用户打命令——写作台账/goals 表全空的同款教训。
    """
    try:
        out = await llm.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "从小说正文中提炼存档。只输出 JSON："
                        '{"summary":"一句话剧情摘要（不超过80字）",'
                        '"threads":["本章埋下/应回收的伏笔，没有则空数组"]}'
                    ),
                },
                {"role": "user", "content": reply[:4000]},
            ],
            temperature=0.2,
            max_tokens=400,
        )
        data = _parse_analysis_json(out)
        summary = str(data.get("summary", "")).strip()
        if not summary:
            return
        threads = data.get("threads")
        upsert_chapter_note(
            chapter_no,
            summary,
            [str(t) for t in threads] if isinstance(threads, list) else [],
            source="auto",
        )
    except Exception:
        pass  # 被动抓取失败静默，绝不影响主回复


# ── 章节分析主流程 ─────────────────────────────────────────

def parse_analysis_command(msg: str) -> str | None:
    m = ANALYSIS_RE.match(msg.strip())
    if m:
        return m.group(1).strip()
    return None


_PROBLEM_TYPES = ("逻辑", "时间线", "动机", "称谓", "设定")
_TYPE_RANK = {t: i for i, t in enumerate(_PROBLEM_TYPES)}
_SEPIA_PROBLEM_TYPES = ("叙事", "话语", "表层")
_SEPIA_TYPE_RANK = {t: i for i, t in enumerate(_SEPIA_PROBLEM_TYPES)}


def parse_problems_json(text: str) -> list[dict]:
    """容错解析问题数组：类型白名单过滤 + 字段截断。"""
    data = _parse_analysis_json(text)
    items = data.get("problems")
    if not isinstance(items, list):
        return []
    out = []
    for item in items[:12]:
        if not isinstance(item, dict):
            continue
        problem = str(item.get("problem", "")).strip()
        if not problem:
            continue
        ptype = str(item.get("type", "逻辑")).strip()
        if ptype not in _PROBLEM_TYPES:
            ptype = "逻辑"
        out.append(
            {
                "type": ptype,
                "quote": str(item.get("quote", "")).strip()[:120],
                "problem": problem[:200],
                "suggestion": str(item.get("suggestion", "")).strip()[:200],
            }
        )
    out.sort(key=lambda c: _TYPE_RANK.get(c["type"], 99))
    return out


def parse_sepia_problems_json(text: str) -> list[dict]:
    """容错解析 Sepia 三类问题：白名单过滤、字段截断、稳定排序。"""
    data = _parse_analysis_json(text)
    items = data.get("sepia_problems")
    if not isinstance(items, list):
        # 兼容少量调用方使用驼峰键，但输出契约仍以 sepia_problems 为准。
        items = data.get("sepiaProblems")
    if not isinstance(items, list):
        return []

    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ptype = str(item.get("type", "")).strip()
        problem = str(item.get("problem", "")).strip()
        if ptype not in _SEPIA_PROBLEM_TYPES or not problem:
            continue
        out.append(
            {
                "type": ptype,
                "quote": str(item.get("quote", "")).strip()[:120],
                "problem": problem[:200],
                "suggestion": str(item.get("suggestion", "")).strip()[:200],
            }
        )
        if len(out) >= 12:
            break
    out.sort(key=lambda c: _SEPIA_TYPE_RANK[c["type"]])
    return out


# 便于内部/旧调用方按私有解析器命名调用。
_parse_sepia_problems_json = parse_sepia_problems_json


def parse_threads_json(text: str) -> list[str]:
    data = _parse_analysis_json(text)
    threads = data.get("threads")
    if not isinstance(threads, list):
        return []
    return [str(t).strip()[:100] for t in threads if str(t).strip()][:10]


def _format_analysis_reply(
    residue: list[tuple[str, str]],
    word_lines: list[str],
    problems: list[dict],
    pacing: str,
    summary: str,
    threads: list[str],
    chapter_no: str | None,
    sepia_problems: list[dict] | None = None,
) -> str:
    """QQ 纯文本回复：表层预检 → 旧五维 → Sepia → 节奏/剧情/伏笔。"""
    lines: list[str] = []
    pre: list[str] = []
    if residue:
        pre.append("残留检测（表层预检，发出去前记得删）：")
        pre.extend(f"  · [{k}] {line}" for k, line in residue[:5])
    if word_lines:
        pre.extend(word_lines)
    if pre:
        lines.extend(pre)
        lines.append("")

    if problems:
        lines.append(f"⚠️ 发现 {len(problems)} 处问题（按严重度排序）：")
        for i, c in enumerate(problems, 1):
            lines.append(f"\n{i}. 【{c['type']}】{c['problem']}")
            if c["quote"]:
                lines.append(f"   你写的：{c['quote']}")
            if c["suggestion"]:
                lines.append(f"   建议：{c['suggestion']}")
    else:
        lines.append("✅ 逻辑/时间线/动机/称谓/设定五个维度未发现问题。")

    sepia_problems = sepia_problems or []
    if sepia_problems:
        lines.append(f"\n⚠️ Sepia 发现 {len(sepia_problems)} 处叙事/话语/表层问题：")
        for i, c in enumerate(sepia_problems, 1):
            lines.append(f"\n{i}. 【{c['type']}】{c['problem']}")
            if c["quote"]:
                lines.append(f"   你写的：{c['quote']}")
            if c["suggestion"]:
                lines.append(f"   建议：{c['suggestion']}")

    if pacing:
        lines.append(f"\n📊 节奏评估：{pacing}")
    if summary:
        lines.append(f"📖 一句话剧情：{summary}")
    if threads:
        lines.append("🧵 本章伏笔：" + "；".join(threads))
    if chapter_no:
        lines.append(f"（第{chapter_no}章摘要已存档，写下一章时我会自动带上前情）")
    return "\n".join(lines).strip()


async def analyze_chapter(text: str, user_id: str | None = None) -> dict:
    """返回 {"reply": str}；识别到章节号时自动 upsert chapter_notes。"""
    text = text.strip()
    # ① 零 LLM 预检
    residue = detect_residue(text)
    word_lines = _word_count_block(text, user_id)

    # ② 权威层 + 前情提要
    novel_facts = knowledge.get_novel_facts(text)
    facts_text = memory.get_facts_injection(user_id=user_id)
    authority, checked_count = _build_authority(novel_facts, facts_text)
    continuity = build_continuity_block()

    system = (
        "你是资深网文章节审校。对照【权威设定】与【前情提要】审读【本章正文】：\n"
        "① 找问题，只报这五类：逻辑（前后矛盾/不合常理）、时间线（时序错乱）、"
        "动机（人物行为缺动机或与性格底色冲突）、称谓（同一对象名称漂移/前后不一致）、"
        "设定（与权威设定冲突）。每条给原文引句和修改建议；没有问题就给空数组，"
        "疑似但不确定的不报。\n"
        "② 评估节奏：本章承载了几个重大事件（死亡/地契/复仇/身份揭示/大冲突各算一个），"
        "判断是否超载——写作原则是" + WORD_TARGET_NOTE + "。\n"
        f"{sepia.build_review_block()}\n"
        "③ Sepia 问题单独放入 sepia_problems，不要混入 problems；每条 type 只能是叙事、话语、表层，"
        "必须有正文原句引文、问题描述和修改建议，无法从正文证明的不要报告。\n"
        '严格输出 JSON 对象：{"summary":"一句话剧情摘要（不超过80字）",'
        '"problems":[{"type":"逻辑|时间线|动机|称谓|设定","quote":"原句",'
        '"problem":"问题描述","suggestion":"修改建议"}],'
        '"sepia_problems":[{"type":"叙事|话语|表层","quote":"原句",'
        '"problem":"问题描述","suggestion":"修改建议"}],'
        '"pacing":"节奏评估（事件数+是否超载）","threads":["本章埋下的伏笔"]}'
    )
    user_parts = [f"【权威设定】\n{authority}"]
    if continuity:
        user_parts.append(continuity)
    user_parts.append(f"【本章正文】\n{text[:4000]}")

    chapter_no = extract_chapter_no(text)
    try:
        out = await llm.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(user_parts)}],
            temperature=0.2,
            max_tokens=2000,
        )
    except Exception as e:
        # LLM 挂了也要把零 LLM 预检结果带回去（这部分不依赖 LLM）。
        head_lines: list[str] = []
        if residue or word_lines:
            head_lines.append("（LLM 调用失败，以下只有预检结果）")
            head_lines.extend(f"· [{k}] {line}" for k, line in residue[:5])
            head_lines.extend(word_lines)
        fallback = f"😅 章节分析暂时没跑通（LLM 调用失败：{type(e).__name__}），稍后再试"
        head = "\n".join(head_lines)
        return {"reply": f"{head}\n{fallback}" if head else fallback}

    parsed = _parse_analysis_json(out)
    problems = parse_problems_json(out)
    sepia_problems = parse_sepia_problems_json(out)
    if not parsed or ("summary" not in parsed and not problems and not sepia_problems):
        # 格式异常时也保留确定性表层预检，避免 LLM 的输出格式遮住已发现的问题。
        head_lines: list[str] = []
        if residue or word_lines:
            head_lines.append("（模型输出格式异常，以下只有预检结果）")
            head_lines.extend(f"· [{k}] {line}" for k, line in residue[:5])
            head_lines.extend(word_lines)
        fallback = "😅 章节分析暂时没跑通（模型输出格式异常），稍后再试"
        head = "\n".join(head_lines)
        return {"reply": f"{head}\n{fallback}" if head else fallback}

    pacing = str(parsed.get("pacing", "")).strip()[:200]
    summary = str(parsed.get("summary", "")).strip()[:200]
    threads = parse_threads_json(out)

    # ③ 识别到章节号 → 自动落档（幂等）
    if chapter_no and summary:
        upsert_chapter_note(chapter_no, summary, threads, source="analysis")

    reply = _format_analysis_reply(
        residue,
        word_lines,
        problems,
        pacing,
        summary,
        threads,
        chapter_no,
        sepia_problems,
    )
    if not problems and checked_count == 0:
        reply += "\n（提示：还没有已确认设定，建议先补设定卡或让我「记住」关键设定）"
    return {"reply": reply}
