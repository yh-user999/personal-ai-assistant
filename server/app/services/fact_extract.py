"""事实自动提取（第 6.21 课）：把用户"确认过的持久设定"搬进 facts 永久层。

背景：设定类内容落在原始聊天流里只靠语义检索召回，换个问法就丢——
"为什么被盯上我说过了"却答不上来。永久事实层（facts，每次注入上下文）
才是"确认过就必须记得"的正确归宿。

策略：用户消息命中确认信号词才触发提取（省 token）；LLM 输出 JSON 三元组，
按 (subject, predicate) 去重 upsert——最新确认覆盖旧值。
"""
import json
import logging
import re

from openai import OpenAIError

from app.core import llm
from app.models.database import connect

logger = logging.getLogger("assistant.fact_extract")

# 触发提取的信号词：用户在这些语境下说的话大概率是持久设定
FACT_SIGNALS = (
    "说过", "记住", "设定", "确定", "就是", "关系", "背景",
    "能力", "性格", "为什么", "改为", "记下", "记录", "底色",
)

EXTRACT_PROMPT = """你是事实提取器。从用户最新消息中提取用户**明确确认或陈述**的持久设定类事实。
规则：
1. 只提取设定类内容：人物关系/性格底色/能力/背景势力/事件原因/数量设定
2. 不提取：闲聊、提问、命令、推测性建议（"可以/可能"开头的不算确认）
3. subject 用具体实体名（如 李羽、少爷、李羽家），predicate 简短（如 性格底色、能力、被盯上原因）
4. 输出 JSON 数组，无内容时输出 []

示例输入："李羽的能力是杀人变强"
输出：[{"subject":"李羽","predicate":"能力","object":"杀人则变强，可全方位提升自身能力"}]"""


def parse_facts_json(text: str) -> list[dict]:
    """容错解析 LLM 输出的 JSON 数组（容忍前后缀噪声）。"""
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
    for item in data:
        if (
            isinstance(item, dict)
            and str(item.get("subject", "")).strip()
            and str(item.get("predicate", "")).strip()
            and str(item.get("object", "")).strip()
        ):
            out.append({
                "subject": str(item["subject"]).strip()[:60],
                "predicate": str(item["predicate"]).strip()[:60],
                "object": str(item["object"]).strip()[:300],
            })
    return out


def upsert_facts(triples: list[dict], user_id: str | None = None) -> int:
    """按 (user_id, subject, predicate) upsert；写入前统一脱敏。"""
    if not triples:
        return 0
    from app.core.memory import normalize_user_id
    from app.services.sanitize import sanitize

    uid = normalize_user_id(user_id)
    triples = [
        {**t, "subject": sanitize(str(t.get("subject", ""))),
         "predicate": sanitize(str(t.get("predicate", ""))),
         "object": sanitize(str(t.get("object", "")))}
        for t in triples
    ]
    conn = connect()
    n = 0
    try:
        for t in triples:
            row = conn.execute(
                "SELECT id FROM facts WHERE user_id=? AND subject=? AND predicate=?",
                (uid, t["subject"], t["predicate"]),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE facts SET object=?, updated_at=? WHERE id=?",
                    (t["object"], _now(), row["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO facts (user_id, subject, predicate, object, confidence, updated_at) "
                    "VALUES (?, ?, ?, ?, 0.9, ?)",
                    (uid, t["subject"], t["predicate"], t["object"], _now()),
                )
            n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# 显式记录指令："将这些记录下来/记一下/先记录"——直接对上一条 AI 回复提取
_RECORD_CMD = re.compile(r"^(?:请|先|把|将|帮(?:我)?)*(?:这些|这个|刚才|上面)?"
                        r"(?:设定|结论|内容)?(?:记录|记)(?:下来|一下|好)?[。！!~～\s]*$")


def is_record_command(msg: str) -> bool:
    """「先将这些记录下来」这类显式记录指令（宽泛匹配，宁滥勿缺——提取器
    本身会过滤无设定内容的消息）。"""
    t = (msg or "").strip()
    return len(t) <= 20 and bool(_RECORD_CMD.search(t))


# 短肯定回复：对上一条 AI 设定提议的确认（"对/好/就这样/行/可以"）
_SHORT_CONFIRM = re.compile(r"^(?:对|好|行|可以|就这样|挺好|就这么定|没问题|OK|ok)[。！!~～\s]*$")


def is_short_confirm(msg: str) -> bool:
    t = (msg or "").strip()
    return bool(t) and len(t) <= 10 and bool(_SHORT_CONFIRM.match(t))


# 设定提议的判定：AI 回复里带这些词才值得在用户确认后提取
_AI_SETTING_HINTS = ("设定", "定下来", "定死", "安排", "就这么定", "结论", "机制")


def last_ai_looks_like_setting(last_ai: str) -> bool:
    return any(h in (last_ai or "") for h in _AI_SETTING_HINTS)


EXTRACT_FROM_AI_PROMPT = """你是事实提取器。用户刚确认了 AI 助手提出的小说设定提议。
从下面的 AI 回复中提取**被用户确认的设定结论**（人物关系/事件走向/能力机制/性格底色等）。
规则：
1. 只提取回复中明确陈述的设定结论；推测、客套、过渡语不提取
2. subject 用具体实体（李羽/老人/豪强…），predicate 简短（如 反击第一步/后续走向/能力机制）
3. 输出 JSON 数组，无内容时输出 []

AI 回复：
{text}
"""


async def extract_from_last_ai(last_ai: str, user_id: str | None = None) -> int:
    """从被用户确认的 AI 设定提议里提取事实（记录指令/短肯定确认时调用）。"""
    try:
        text = await llm.chat(
            [
                {"role": "system", "content": "你是事实提取器，只输出 JSON 数组。"},
                {"role": "user", "content": EXTRACT_FROM_AI_PROMPT.replace("{text}", last_ai[:2000])},
            ],
            temperature=0,
            max_tokens=400,
        )
    except OpenAIError as e:
        logger.warning("AI 回复设定提取 LLM 失败: %s", e)
        return 0
    triples = parse_facts_json(text or "")
    if triples:
        logger.info("从 AI 回复提取设定 %d 条: %s", len(triples),
                    [(t["subject"], t["predicate"]) for t in triples])
    return upsert_facts(triples, user_id=user_id)


async def maybe_extract_facts(user_msg: str, user_id: str | None = None) -> int:
    """命中信号词则提取并写入 facts；返回写入条数（未触发返回 0）。"""
    if not any(s in user_msg for s in FACT_SIGNALS):
        return 0
    try:
        text = await llm.chat(
            [
                {"role": "system", "content": EXTRACT_PROMPT},
                {"role": "user", "content": user_msg[:2000]},
            ],
            temperature=0,
            max_tokens=400,
        )
    except OpenAIError as e:
        logger.warning("事实提取 LLM 调用失败: %s", e)
        return 0
    triples = parse_facts_json(text or "")
    if triples:
        logger.info("提取到持久事实 %d 条: %s", len(triples),
                    [(t["subject"], t["predicate"]) for t in triples])
    return upsert_facts(triples, user_id=user_id)
