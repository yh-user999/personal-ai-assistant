"""成长感知：用事实回答"我感觉没收获"。

## 动因（用户原话）

- 「今天没什么心思工作和学习，怎么办」
- 「我每天工作学习耐心都不足，能怎么办」
- 「我也没做什么，都是交给ai做，自己感觉没什么收获」

这类问题在他 369 条消息里反复出现，而**现有模块一个都答不上**——她只能泛泛
安慰。但数据其实都在：3369 条行为事件（git 提交、应用时长）、每日小结、
consolidation 提取的话题演进、知识库灌入记录。

## 设计原则：事实反证，不做鸡汤

「都是交给 AI 做的，自己没收获」是个**真问题**，不能用"你已经很努力了"糊过去。
正确回应是指出他忽略的那部分收获（判断、决策、方案选择），并给出可核对的
数字。所以这个模块只负责**取出证据**，不负责下结论——结论交给 LLM，但它手里
必须有事实。

零 LLM：全部是 SQL 聚合 + 规则组装。
"""
import json
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.models.database import connect

logger = logging.getLogger("assistant.growth")

TZ = ZoneInfo("Asia/Shanghai")

# 触发成长感知注入的信号：自我否定 / 状态低落 / 收获质疑
# 自我否定信号。用正则而非固定短语——中文口语常在词间插字，实测
# 「没什么收获」「耐心都不足」「没什么心思」都会漏掉写死的短语。
# 刻意不含"怎么办"：那太宽，「平台期怎么办」「报错怎么办」都会误触发。
SELF_DOUBT_RE = re.compile(
    r"没(?:什么|啥|多少)?(?:收获|进步|成长|长进)"
    r"|没(?:什么|啥)?(?:心思|动力|耐心|精神)"
    r"|(?:耐心|动力|精力)(?:都)?不足"
    r"|没(?:做|干)(?:什么|啥|多少)"
    r"|白费|一无所获|浪费时间|虚度|坚持不(?:下|了)|静不下"
    r"|迷茫|焦虑|空虚|效率低|提不起劲"
    r"|(?:都是|交给)\s*[aA][iI]"
)

# 不算"产出"的应用（游戏/娱乐）——统计时单列，不混进工作时长
LEISURE_APPS = ("dota", "steam", "game", "wegame", "bilibili", "youku", "iqiyi")

DEFAULT_DAYS = 7


def _empty_evidence(days: int) -> dict:
    return {
        "days": days,
        "commits": 0,
        "work_hours": 0.0,
        "leisure_hours": 0.0,
        "top_apps": [],
        "topics": [],
        "topic_count": 0,
        "turns": 0,
        "work_logs": [],
        "summaries": [],
        "ingested_docs": [],
        "corrections": 0,
    }


def detect_self_doubt(text: str) -> bool:

    """是否在质疑自己的收获/状态。"""
    return bool(SELF_DOUBT_RE.search(text or ""))


def _utc_cutoff(days: int) -> str:
    return (datetime.now(TZ) - timedelta(days=days)).astimezone(timezone.utc).isoformat()


def _local_cutoff_date(days: int) -> str:
    return (datetime.now(TZ) - timedelta(days=days)).date().isoformat()


def collect_evidence(days: int = DEFAULT_DAYS, user_id: str | None = None) -> dict:
    """近 N 天的成长证据。纯 SQL 聚合，不含任何判断。

    成长证据来自主人专属行为/台账；访客调用时返回空结果，且记忆查询仍
    使用统一 user scope，避免把主人数据当成访客证据。
    """
    from app.core.memory import _user_scope, is_owner_user, normalize_user_id

    days = max(1, min(int(days), 90))
    uid = normalize_user_id(user_id)
    if not is_owner_user(uid):
        return _empty_evidence(days)
    clause, args = _user_scope(uid)
    cutoff_utc = _utc_cutoff(days)
    cutoff_date = _local_cutoff_date(days)
    conn = connect()
    try:
        commits = conn.execute(
            f"SELECT COUNT(*) AS c FROM behavior_events "
            f"WHERE kind='git_commit' AND start_ts >= ? AND {clause}",
            (cutoff_utc, *args),
        ).fetchone()["c"]

        apps = conn.execute(
            f"SELECT name, SUM((julianday(end_ts)-julianday(start_ts))*24) AS h "
            f"FROM behavior_events WHERE kind='app_usage' AND start_ts >= ? "
            f"AND end_ts IS NOT NULL AND {clause} GROUP BY name ORDER BY h DESC LIMIT 12",
            (cutoff_utc, *args),
        ).fetchall()

        # 话题演进：consolidation 提取的 topics
        topic_rows = conn.execute(
            f"SELECT topics FROM memories WHERE topics NOT IN ('', '[]') "
            f"AND topics IS NOT NULL AND ts >= ? AND {clause}",
            (cutoff_utc, *args),
        ).fetchall()

        turns = conn.execute(
            f"SELECT COUNT(*) AS c FROM memories WHERE sender='user' AND ts >= ? AND {clause}",
            (cutoff_utc, *args),
        ).fetchone()["c"]

        # DISTINCT：work_log 有重复行（测试灌入过 18 条同样的"下午2-4点调参"），
        # 不去重会让注入里出现三遍同一句手记
        logs = conn.execute(
            f"SELECT DISTINCT date, content FROM work_log WHERE date >= ? AND {clause} "
            "ORDER BY date DESC LIMIT 10",
            (cutoff_date, *args),
        ).fetchall()

        summaries = conn.execute(
            f"SELECT date, content FROM daily_summaries WHERE date >= ? AND {clause} ORDER BY date DESC",
            (cutoff_date, *args),
        ).fetchall()

        # 知识库灌入（学习投入的直接证据）
        docs = conn.execute(
            "SELECT doc_name, COUNT(*) AS n FROM knowledge_chunks "
            "WHERE created_at >= ? GROUP BY doc_name ORDER BY n DESC LIMIT 5",
            (cutoff_utc,),
        ).fetchall()

        # 被纠正次数：说明在校准她，这本身是判断力的体现
        lessons = conn.execute(
            f"SELECT COUNT(*) AS c FROM lessons WHERE created_at >= ? AND {clause}",
            (cutoff_utc, *args),
        ).fetchone()["c"]
    finally:
        conn.close()

    topics: Counter = Counter()
    for r in topic_rows:
        try:
            topics.update(t for t in json.loads(r["topics"]) if t)
        except (TypeError, ValueError):
            continue

    work_hours = 0.0
    leisure_hours = 0.0
    top_work: list[tuple[str, float]] = []
    for a in apps:
        h = round(a["h"] or 0, 1)
        if any(k in (a["name"] or "").lower() for k in LEISURE_APPS):
            leisure_hours += h
        else:
            work_hours += h
            top_work.append((a["name"], h))

    return {
        "days": days,
        "commits": commits,
        "work_hours": round(work_hours, 1),
        "leisure_hours": round(leisure_hours, 1),
        "top_apps": top_work[:4],
        "topics": [t for t, _ in topics.most_common(6)],
        "topic_count": len(topics),
        "turns": turns,
        "work_logs": [dict(r) for r in logs],
        "summaries": [dict(r) for r in summaries],
        "ingested_docs": [(r["doc_name"], r["n"]) for r in docs],
        "corrections": lessons,
    }


def has_evidence(ev: dict) -> bool:
    """是否有足够证据说话——没有就别硬凑（宁可不注入）。"""
    return bool(
        ev.get("commits") or ev.get("topics") or ev.get("work_logs")
        or ev.get("ingested_docs") or ev.get("work_hours", 0) >= 1
    )


def build_injection(days: int = DEFAULT_DAYS, user_id: str | None = None) -> str:
    """成长证据注入。无证据返回空串。

    只给事实与解读方向，不给结论——结论由 LLM 结合当下语境说，
    但它手里必须有可核对的数字，否则又变成空泛安慰。
    """
    ev = collect_evidence(days, user_id=user_id)
    if not has_evidence(ev):
        return ""

    parts: list[str] = []
    if ev["commits"]:
        parts.append(f"git 提交 {ev['commits']} 次")
    if ev["work_hours"] >= 1:
        apps = "、".join(f"{n} {h}h" for n, h in ev["top_apps"])
        parts.append(f"工作类应用 {ev['work_hours']}h（{apps}）")
    if ev["topic_count"]:
        parts.append(f"聊过 {ev['topic_count']} 个不同话题（{'、'.join(ev['topics'])}）")
    if ev["turns"]:
        parts.append(f"对话 {ev['turns']} 轮")
    if ev["ingested_docs"]:
        docs = "、".join(f"{d}（{n} 块）" for d, n in ev["ingested_docs"])
        parts.append(f"新灌入资料：{docs}")
    if ev["corrections"]:
        parts.append(f"纠正过我 {ev['corrections']} 次")
    if ev["work_logs"]:
        recent = "；".join(f"{r['date']} {r['content'][:24]}" for r in ev["work_logs"][:3])
        parts.append(f"手记：{recent}")

    body = "\n".join(f"{p}" for p in parts)
    leisure = (
        f"\n（同期娱乐类应用 {ev['leisure_hours']}h，如果他在意投入产出比可以顺带提）"
        if ev["leisure_hours"] >= 2 else ""
    )
    return (
        f"【近 {ev['days']} 天的实际产出（用户正在质疑自己的收获，"
        f"用这些事实回应，不要空泛安慰）】\n"
        f"{body}{leisure}\n"
        "回应要点：① 先给具体数字，让他知道自己实际做了什么 "
        "② 如果他说「都是 AI 做的所以没收获」，指出他做的是判断与决策"
        "（选方案、发现问题、纠正方向），这部分不可替代且没有记录在代码行数里 "
        "③ 不要说「你已经很努力了」这类话，他要的是事实不是安慰 "
        "④ 数字之外可以指出一个他可能忽略的具体进展"
    )
