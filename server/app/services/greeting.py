"""个性化问候：按时间/日期/行为数据生成，每次打开面板实时刷新。

零 LLM 成本（规则组装）；数据源：本地时间、git 提交（行为注入同款）、周报安排。
"""
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.behavior_context import get_today_commits

TZ = ZoneInfo("Asia/Shanghai")

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_GREETINGS = {
    "dawn": ["凌晨好，这么晚还没睡？", "夜深了，注意休息 🌙", "凌晨还在忙吗？"],
    "morning": ["早上好 ☀️", "早安，新的一天", "早上好，昨晚睡得好吗？"],
    "noon": ["中午好，记得吃饭 🍚", "中午好，该休息一下了", "中午好，别一直对着屏幕"],
    "afternoon": ["下午好 ☕", "下午好，喝杯水休息下", "下午好，今天的进展如何？"],
    "evening": ["晚上好 🌆", "晚上好，一天辛苦了", "晚上好，收工了吗？"],
    "night": ["晚上好，夜深了 🌙", "晚上好，注意别熬太晚"],
}


def get_greeting() -> str:
    """生成个性化问候（问候语 + 时效信息）。"""
    now = datetime.now(TZ)
    hour = now.hour
    if hour < 5:
        base = random.choice(_GREETINGS["dawn"])
    elif hour < 9:
        base = random.choice(_GREETINGS["morning"])
    elif hour < 12:
        base = random.choice(_GREETINGS["noon"])
    elif hour < 18:
        base = random.choice(_GREETINGS["afternoon"])
    elif hour < 23:
        base = random.choice(_GREETINGS["evening"])
    else:
        base = random.choice(_GREETINGS["night"])

    # 时效信息
    extras = []
    weekday = _WEEKDAYS[now.weekday()]
    if weekday == "周日":
        extras.append("今晚 21:00 会自动生成本周学习反思")
    elif weekday == "周六":
        extras.append("周六快乐，适当休息 🎮")
    elif weekday == "周五":
        extras.append("周五啦，明天可以放松一下")
    commits = get_today_commits()
    if commits:
        # "今天 git 提交 N 次（最近：...）" → 精简为 "今天已提交 N 次代码"
        import re
        m = re.search(r"提交 (\d+) 次", commits)
        if m:
            extras.append(f"今天已提交 {m.group(1)} 次代码，状态不错 💪")

    date_str = f"{now.month}月{now.day}日 {weekday}"
    parts = [base, f"今天是 {date_str}"]
    if extras:
        parts.append(" · ".join(extras))
    return " | ".join(parts)
