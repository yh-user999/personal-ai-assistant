"""小说设定卡种子数据（第 6.19 课：策划事实层，可重复执行 = 全量替换）。

条目依据原文检索验证后手工策划：人物身份/别名/关键事件。
后续新增小说或补充设定时直接在此文件追加条目后重跑。
"""
import sys
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))

from app.models.database import connect

_NOW = "2026-08-28T00:00:00+00:00"

SEEDS = [
    # ── 《寂静杀戮》──
    {
        "book": "小说-寂静杀戮",
        "keywords": "左志诚,左擎苍,夜海",
        "content": (
            "左志诚即左擎苍（同一人的两个名字）。他的左眼中有夜海命丛——"
            "七大神命丛之一，可吸收光能，是修炼道术的根基命丛。"
        ),
    },
    {
        "book": "小说-寂静杀戮",
        "keywords": "蜃宗,南圣门,挖走",
        "content": (
            "蜃宗：南圣门门主、魔道命丛研究者。曾分神化念附体古墓老者；"
            "亲手挖走左擎苍（左志诚）左眼夜海命丛，动机是据为己有——"
            "他说「七大神命丛之一，留在你身上倒是浪费了，就交给我吧」，"
            "挖出后如赏艺术品般把玩。其后持有夜海并兼修夜亡君主的命图，"
            "为全书主要反派。"
        ),
    },
    # ── 《食物链顶端的男人》──
    {
        "book": "小说-食物链顶端的男人",
        "keywords": "李安平",
        "content": (
            "李安平：本书主角。超能力为念气（黑色念动力），"
            "可在身后凝聚黑色人影、隔空操控物体，属战斗类型能力者；"
            "能力成长线贯穿全书。"
        ),
    },
]


def main() -> None:
    conn = connect()
    conn.execute("DELETE FROM novel_facts")
    for s in SEEDS:
        conn.execute(
            "INSERT INTO novel_facts (book, keywords, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            (s["book"], s["keywords"], s["content"], _NOW),
        )
    conn.commit()
    conn.close()
    print(f"设定卡已写入 {len(SEEDS)} 条")


if __name__ == "__main__":
    main()
