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


def clear(group_id: str | None = None) -> None:
    """清空指定群或全部群的内存上下文。"""
    if group_id is None:
        _store.clear()
        return
    _store.pop(str(group_id).strip(), None)


def tracked_groups() -> int:
    """当前跟踪的群数量（诊断用，不含内容）。"""
    return len(_store)
