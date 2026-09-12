"""群聊短期上下文：仅进程内存，不落库、不检索、不建画像。

隐私边界（实现约束，不是建议）：
- **绝不写数据库**：不进 memories/facts/profile 等任何表，因此不会被检索命中，
  也不会进入备份、导出或向量索引。
- **仅保留最近若干轮**：按群 FIFO 截断，只为接住"那你说说"这类追问。
- **不建群成员档案**：发言人只保留一个短的稳定别名（哈希前 6 位），
  不存 QQ 号、昵称，避免形成可累积的人物画像。
- **有生存期**：超过 TTL 的条目读取时即丢弃；进程重启全部清空。
- **总量上限**：群数与单群条数都有上限，防止长期占用内存。

这样群聊能接住上下文，但不会变成"记录群成员所有聊天记录"的系统。
"""
from __future__ import annotations

import hashlib
import re
import time
from collections import OrderedDict

# 每群保留的消息条数（用户+助手合计）。够接住追问，不足以拼出长期画像。
MAX_TURNS_PER_GROUP = 8
# 单条最大字符数，超出截断，避免长文占内存。
MAX_CHARS_PER_ITEM = 500
# 上下文生存期：超时即视为新话题，避免隔天还接着旧对话。
TTL_SECONDS = 30 * 60
# 最多跟踪的群数量，超出淘汰最久未活跃的群。
MAX_GROUPS = 64

# group_id -> list[(timestamp, role, speaker_alias, text)]
_store: OrderedDict[str, list[tuple[float, str, str, str]]] = OrderedDict()

_SCENE_EMOTION_RE = re.compile(r"焦虑|难受|崩溃|烦|累|沮丧|生气|压力|睡不着|委屈|吵|气死")
_SCENE_TECH_RE = re.compile(r"代码|接口|报错|服务器|数据库|部署|配置|脚本|模型|程序|bug|API", re.IGNORECASE)
_SCENE_CELEBRATION_RE = re.compile(r"哈哈+|笑死|太爽|绝了|好耶|恭喜|成功|赢了|舒服")
_SCENE_TENSE_RE = re.compile(r"不对|别吵|闭嘴|滚|冲突|争议|谁的错|骗子|垃圾")
_SCENE_QUESTION_RE = re.compile(r"[?？]|吗[？?。！!\s]*$|(?:怎么|为什么|是否|能不能|有没有|哪个|哪些|什么)")
_SCENE_SWITCH_RE = re.compile(r"换个话题|另外|顺便(?:问|说)|对了|再问一个|先不说|不聊这个了")


def speaker_alias(user_id: str) -> str:
    """把发言人映射为短别名：可区分"谁在说"，但不保留身份本身。

    不存 QQ 号或昵称，避免跨轮累积成人物档案；同一群内稳定，便于模型区分说话人。
    """
    raw = str(user_id or "").strip()
    if not raw:
        return "某人"
    return "成员" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:6]


def _prune(items: list[tuple[float, str, str, str]], now: float) -> list[tuple[float, str, str, str]]:
    fresh = [item for item in items if now - item[0] <= TTL_SECONDS]
    return fresh[-MAX_TURNS_PER_GROUP:]


def remember(group_id: str, role: str, text: str, *, user_id: str = "", now: float | None = None) -> None:
    """记录一轮群对话到内存。role 为 'user' 或 'assistant'。"""
    group = str(group_id or "").strip()
    content = " ".join(str(text or "").split())[:MAX_CHARS_PER_ITEM]
    if not group or not content:
        return
    current = time.time() if now is None else float(now)
    alias = speaker_alias(user_id) if role == "user" else "小月"
    items = _prune(_store.get(group, []), current)
    items.append((current, role, alias, content))
    _store[group] = items[-MAX_TURNS_PER_GROUP:]
    _store.move_to_end(group)
    while len(_store) > MAX_GROUPS:
        _store.popitem(last=False)


def recent_messages(group_id: str, *, now: float | None = None) -> list[dict[str, str]]:
    """取该群最近上下文，供 prompt 使用；过期自动丢弃。"""
    group = str(group_id or "").strip()
    if not group:
        return []
    current = time.time() if now is None else float(now)
    items = _prune(_store.get(group, []), current)
    if items:
        _store[group] = items
    else:
        _store.pop(group, None)
        return []
    return [
        {"role": role, "content": (f"{alias}：{text}" if role == "user" else text)}
        for _ts, role, alias, text in items
    ]


def _grams(text: str) -> set[str]:
    clean = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", text or "")
    return {clean[i : i + 2] for i in range(max(0, len(clean) - 1))}


def scene_summary(group_id: str, *, now: float | None = None) -> dict[str, object]:
    """从现有短期上下文派生有限群场景摘要，不保存额外身份或原文。"""
    group = str(group_id or "").strip()
    if not group:
        return {}
    current = time.time() if now is None else float(now)
    items = _prune(_store.get(group, []), current)
    if not items:
        _store.pop(group, None)
        return {}
    _store[group] = items
    _store.move_to_end(group)

    texts = [item[3] for item in items]
    joined = " ".join(texts)
    if _SCENE_EMOTION_RE.search(joined):
        atmosphere = "emotional"
    elif _SCENE_TENSE_RE.search(joined):
        atmosphere = "tense"
    elif _SCENE_CELEBRATION_RE.search(joined):
        atmosphere = "celebration"
    elif _SCENE_TECH_RE.search(joined):
        atmosphere = "technical"
    elif _SCENE_QUESTION_RE.search(texts[-1]):
        atmosphere = "questioning"
    else:
        atmosphere = "casual"

    prior_text = ""
    for _ts, role, _alias, text in reversed(items[:-1]):
        if role == "user":
            prior_text = text
            break
    current_grams = _grams(texts[-1])
    prior_grams = _grams(prior_text)
    topic_shift = bool(
        _SCENE_SWITCH_RE.search(texts[-1])
        or (len(current_grams) >= 3 and len(prior_grams) >= 3 and not current_grams.intersection(prior_grams))
    )
    return {
        "message_count": len(items),
        "speaker_count": len({alias for _ts, role, alias, _text in items if role == "user"}),
        "last_role": items[-1][1],
        "atmosphere": atmosphere,
        "topic_shift": topic_shift,
        "has_recent_bot_reply": any(role == "assistant" for _ts, role, _alias, _text in items[-4:]),
    }


def clear(group_id: str | None = None) -> None:
    """清空指定群或全部群的内存上下文。"""
    if group_id is None:
        _store.clear()
        return
    _store.pop(str(group_id).strip(), None)


def tracked_groups() -> int:
    """当前跟踪的群数量（诊断用，不含内容）。"""
    return len(_store)
