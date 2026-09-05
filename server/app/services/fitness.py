"""健身减脂助手（第 6.29 课）：健身台账 + 健身知识卡检索。

- 健身台账（零 LLM，复用写作台账模式）：
  `记录体重：70.5` / `训练记录：深蹲5x5 卧推60kg` / `健身进度`
- 健身知识卡（fitness_facts 表，仿小说设定卡）：
  关键词触发 → 作为"权威资料"注入，条目自带出处年份（2024/2025 指南）。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.models.database import connect

logger = logging.getLogger("assistant.fitness")

TZ = ZoneInfo("Asia/Shanghai")

WEIGHT_RE = re.compile(
    r"^(?:记录)?体重[:：]?\s*(\d{2,3}(?:\.\d)?)\s*(?:公斤|kg|千克)?$"
)
TRAIN_RE = re.compile(r"^(?:训练|健身)记录[:：]?\s*(.+)$")
PROGRESS_WORDS = ("健身进度", "健身统计", "健身台账", "健身记录查询")


def _now_utc() -> str:
    from app.common.timeutil import utc_str
    return utc_str()


def parse_weight(msg: str) -> float | None:
    m = WEIGHT_RE.match(msg.strip())
    if not m:
        return None
    w = float(m.group(1))
    return w if 20.0 <= w <= 300.0 else None


def parse_training(msg: str) -> str | None:
    """解析训练记录命令；进度查询词显式排除（不依赖路由顺序）。"""
    stripped = msg.strip()
    if stripped in PROGRESS_WORDS:
        return None
    m = TRAIN_RE.match(stripped)
    if m:
        detail = m.group(1).strip().lstrip("：:，,").strip()
        if detail:
            return detail[:200]
    return None


def add_log(kind: str, value: float | None, detail: str) -> int:
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO fitness_log (kind, value, detail, created_at) VALUES (?, ?, ?, ?)",
            (kind, value, detail, _now_utc()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def fitness_summary() -> str:
    """健身进度汇总：体重趋势 + 近 7 天训练 + 最近记录。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT kind, value, detail, created_at FROM fitness_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return "🏋️ 还没有健身记录。用「记录体重：70.5」和「训练记录：练了什么」开始记账吧。"

    weights = [r for r in rows if r["kind"] == "weight" and r["value"] is not None]
    trainings = [r for r in rows if r["kind"] == "training"]
    today = datetime.now(TZ).date()
    week_trainings = [
        r
        for r in trainings
        if (today - _row_local(r).date()).days < 7
    ]

    lines = ["🏋️ 健身台账："]
    if weights:
        latest = weights[0]
        first = weights[-1]
        delta = round(latest["value"] - first["value"], 1)
        trend = f"{delta:+.1f} kg" if delta else "持平"
        lines.append(f"  当前体重：{latest['value']} kg（起始 {first['value']} kg，变化 {trend}）")
    if trainings:
        lines.append(f"  训练记录：共 {len(trainings)} 次，近 7 天 {len(week_trainings)} 次")
        if not week_trainings:
            lines.append("  ⚠️ 近 7 天没有训练记录——停练恢复期先动起来比练得多重要")
    else:
        lines.append("  ⚠️ 训练记录：还没有。停练一个月后恢复期，建议从每周 2-3 次低强度开始")
    lines.append("  最近记录：")
    for r in rows[:5]:
        dt = _row_local(r).strftime("%m-%d")
        if r["kind"] == "weight":
            lines.append(f"  {dt} 体重 {r['value']} kg")
        else:
            lines.append(f"  {dt} 训练：{r['detail']}")
    return "\n".join(lines)


def _row_local(row) -> datetime:
    dt = datetime.fromisoformat(row["created_at"])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(TZ)


# ── 健身知识卡（仿小说设定卡：关键词触发 → 权威资料注入）────

def get_fitness_facts(query: str) -> list[str]:
    """query 命中关键词的健身知识卡条目（带出处）。"""
    conn = connect()
    try:
        rows = conn.execute("SELECT keywords, content FROM fitness_facts").fetchall()
    finally:
        conn.close()
    matched = []
    for r in rows:
        for kw in r["keywords"].replace("，", ",").split(","):
            if kw.strip() and kw.strip() in query:
                matched.append(r["content"])
                break
    return matched


def seed_fitness_cards() -> int:
    """知识卡种子数据（第 6.29 课）。幂等：按 keywords 查重后插入。"""
    cards = [
        # (keywords, content)
        ("热量缺口,减脂,瘦",
         ("热量缺口：每日摄入低于总消耗 300~500 kcal，每周约减 0.25~0.5 kg；"
         "过大的缺口（>800 kcal）掉肌肉、易反弹，不建议（《成人肥胖食养指南（2024年版）》）。")),
        ("蛋白质,蛋白,鸡胸肉,肉蛋奶",
         ("减脂期蛋白质摄入建议 1.2~1.6 g/kg/天（约体重×1.4 g），优先瘦肉/蛋/奶/豆制品，"
         "保肌肉、增强饱腹感（《肥胖症诊疗指南（2025年版）》个体化营养治疗要点）。")),
        ("主食,碳水,米饭,馒头",
         ("碳水不戒：主食粗细搭配，全谷物/杂豆占 1/3~1/2；减脂期可适度减少精制主食而非砍光，"
         "长期低碳易疲劳、掉肌肉（《中国居民膳食指南（2022）》+ 食养指南 2024）。")),
        ("蔬菜,水果,膳食纤维",
         ("每天蔬菜 300~500 g（深色占一半以上）、水果 200~350 g；膳食纤维增强饱腹感，"
         "减脂期蔬菜可放开吃（《中国居民膳食指南（2022）》）。")),
        # 触发词要覆盖真实说法：实测"我要的是每个部位四个动作，一个动作四组"
        # 命中 0 张卡——用户说"练胸/卧推/几组"，而卡上写的是"力量训练/抗阻"。
        ("力量训练,撸铁,抗阻,肌肉,练胸,练背,练腿,练肩,练手臂,卧推,深蹲,硬拉,划船,弯举",
         ("减脂期间力量训练优先：保留肌肉、提高基础代谢；复合动作为主（深蹲/推/拉/硬拉类），"
         "渐进超负荷（重量或次数小幅递增）是核心原则（ACSM《运动测试与处方指南》第12版，2025）。")),
        ("有氧,跑步,快走,骑车",
         ("有氧建议：每周 150~300 分钟中等强度或 75~150 分钟高强度，"
         "快走/慢跑/骑车都算；配合每周 ≥2 次力量训练效果最佳（WHO 体力活动指南 2020）。")),
        ("平台期,不掉秤,停滞",
         ("平台期（2 周以上不掉秤）：先核查真实摄入（记录饮食 3 天）、增加日常活动量（NEAT）、"
         "调整训练变量（强度/时长/方式），保证睡眠与压力管理；不是简单继续少吃"
         "（《肥胖症诊疗指南（2025年版）》+ 食养指南 2024）。")),
        ("久坐,上班,办公室",
         ("久坐抵消运动收益：每 30~60 分钟起身活动 2~3 分钟（接水/走动/拉伸），"
         "日常活动量（NEAT）对总消耗的贡献常大于刻意训练（WHO 2020 + 食养指南 2024）。")),
        ("睡眠,熬夜,失眠",
         ("睡眠不足（<6 小时）升高饥饿素、抑制瘦素，食欲失控且恢复变差；减脂期保证 7~9 小时"
         "（《肥胖症诊疗指南（2025年版）》生活方式干预）。")),
        ("喝水,饮水,水分",
         ("每天饮水 1500~1700 ml（约 7~8 杯），运动后适量补水；饮水不足会影响代谢与运动表现"
         "（《中国居民膳食指南（2022）》）。")),
        ("停练,恢复,重新开始,复训",
         ("停练一个月恢复：前 1~2 周用原训练量的 50~70% 找回动作与节奏，再逐步加回；"
         "直接上原重量易受伤、易挫败（ACSM 训练学原则：可逆性与渐进性）。")),
        ("体脂,围度,体重波动",
         ("体重单日波动 1~2 kg 属正常（水分/食物残渣/钠摄入）；减脂看趋势线（周均值）"
         "与围度（腰围）变化，别被单日数字带情绪（2025 肥胖诊疗指南：身体成分监测）。")),
        ("bmi,超重,肥胖",
         ("中国成人 BMI 分级：18.5~23.9 正常、24~27.9 超重、≥28 肥胖；"
         "BMI 24 以上建议控重（《中国超重/肥胖医学营养治疗指南（2021）》）。")),
        ("误区,节食,断食,减肥药",
         ("常见误区：不吃主食/极端节食（掉肌肉+反弹）、只做有氧不练力量（保不住代谢）、"
         "指望减肥产品（未经医学评估别碰）——可持续的缺口+力量+睡眠才是长期答案"
         "（2024 食养指南 + 2025 肥胖诊疗指南）。")),
        ("饮食记录,记录饮食,吃了什么,饮食打卡,热量记录,记餐",
         ("饮食记录是减脂性价比最高的工具：记录 3~7 天就能暴露隐形热量（饮料/酱料/零食）；"
         "先记录再调整，比凭感觉少吃可靠（2025 肥胖诊疗指南：行为干预）。")),

        # ── 训练学（补齐"排计划"场景的依据）──────────────────
        # 动因：用户来问"每个部位四个动作、一个动作四组、12 次"时，
        # 原有 15 张卡全是营养/生活方式，没有一条能用来评估训练容量与
        # 动作选择——她只能顺着用户说"12 次可以吗"，把判断推回去。
        # 触发词含具体数量说法（"四组/4组/三组"）：用户很少说"组数是多少"，
        # 更常直接给数字——"一个动作四组"
        (("组数,几组,多少组,容量,训练量,一周几练,频率,练几次,"
         "三组,四组,五组,3组,4组,5组"),
         ("每肌群每周有效容量约 10~20 组（新手 10~12 组即可进步，进阶者可到 20 组）；"
         "同一肌群每周练 2 次优于 1 次（同容量下分两天做效果更好）。"
         "单次练同一肌群超过 10 组，后面几组质量下降、性价比低"
         "（ACSM 抗阻训练指南 + 训练容量-反应研究综述）。")),
        ("次数,做几次,12次,8次,rep,重量,几kg,力竭",
         ("次数区间按动作类型分：复合动作（卧推/深蹲/硬拉/划船）用 6~12 次、"
         "接近但不到力竭（留 1~2 次余力）；孤立动作（夹胸/弯举/侧平举）用 10~15 次。"
         "全部动作一律 12 次不合理——复合动作用大重量低次数刺激更有效"
         "（ACSM：肌肥大 6~12 RM，力量 ≤6 RM）。")),
        ("复合动作,孤立动作,动作选择,动作安排,四个动作,几个动作",
         ("动作安排：每个部位 2~4 个动作即可，以复合动作打头（力气最足时做最难的），"
         "孤立动作收尾补足。同一部位堆 3 个以上同模式动作（如卧推+上斜卧推+双杠臂屈伸"
         "都是推类）会让容量虚高而刺激重复，不如换一个不同角度或不同肌群"
         "（ACSM：复合动作优先原则）。")),
        # 触发词必须含具体动作名：用户列训练计划时不会说"恢复/隔天/分化"
        # 这些术语，只会写"双杠臂屈伸""绳索下压"——而风险恰恰藏在他没说出
        # 的那层（三头被卧推和双杠重复征用）。靠术语触发等于永远不命中。
        (("恢复,间隔,隔天,连着练,分化,推拉腿,练完酸痛,肌肉酸痛,"
         "臂屈伸,下压,三头,二头,肱三头,肱二头,手臂,窄距,飞鸟,夹胸"),
         ("同一肌群需 48~72 小时恢复。安排计划要看「间接征用」："
         "练胸（卧推/双杠臂屈伸）已大量用到肱三头，隔天再单独练三头恢复不足；"
         "练背（划船/下拉）同理会用到肱二头。常见解法是推/拉/腿分化，"
         "或把手臂放在同模式日之后而非隔天（ACSM：恢复与超量恢复原则）。")),
        ("新手,初学,刚开始练,练了多久,训练年限,进阶",
         ("训练年限决定容量与方案复杂度：新手（<6 个月）用全身或上下肢分化、"
         "每肌群每周 10~12 组、动作数 2~3 个即可稳定进步，此时加量不如加熟练度；"
         "进阶者（1 年以上）才需要更细的分化与更高容量"
         "（ACSM：渐进超负荷应优先于容量堆叠）。")),
        ("热身,拉伸,受伤,伤,protect,关节",
         ("训练前做 5~10 分钟动态热身 + 目标动作空杆/轻重量 1~2 组；"
         "静态拉伸放训练后。大重量复合动作前热身不足是最常见的受伤原因"
         "（ACSM：热身与损伤预防）。")),
    ]
    conn = connect()
    try:
        added = updated = 0
        for keywords, content in cards:
            # 幂等判据用 content，不用 keywords：卡的身份是它的内容，
            # keywords 只是索引。原实现按 keywords 全串精确匹配——扩充触发词
            # 等于换了主键，会把同一张卡再插一份，查询时两张都命中、
            # prompt 出现重复注入。实测库里第 15 张已因此与代码不一致。
            row = conn.execute(
                "SELECT id, keywords FROM fitness_facts WHERE content=?", (content,)
            ).fetchone()
            if row:
                if row["keywords"] != keywords:  # 触发词有更新 → 改索引不插新行
                    conn.execute(
                        "UPDATE fitness_facts SET keywords=? WHERE id=?",
                        (keywords, row["id"]),
                    )
                    updated += 1
                continue
            conn.execute(
                "INSERT INTO fitness_facts (book, keywords, content, created_at) "
                "VALUES ('健身', ?, ?, ?)",
                (keywords, content, _now_utc()),
            )
            added += 1
        conn.commit()
        if updated:
            logger.info("健身知识卡触发词更新 %d 张", updated)
        return added
    finally:
        conn.close()
