"""用户小说设定补录（一次性 + 可复用）：把已确认的设定搬进 facts 永久层。

背景：用户抱怨"确认过的设定机器人记不住"——查证后这些设定只存在
原始聊天流（#214/#447 等），未进永久事实层。本脚本从聊天记录人工
核对后补录；以后新设定由 fact_extract 自动管道接管。
用法：cd server && .venv/bin/python scripts/seed_story_facts.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.fact_extract import upsert_facts

# 经核对聊天记录确认的设定（8-28）：来源 #214/#447/#473/#493/#505
STORY_FACTS = [
    {"subject": "李羽", "predicate": "性格底色", "object": "正直、正义的绝对主义者（一人之力要让世界按他的正义运转）"},
    {"subject": "李羽", "predicate": "被盯上原因", "object": "家里的田恰卡在地方豪强要连片的地中间，挡人财路；原身不肯卖地，对方下死手"},
    {"subject": "李羽", "predicate": "能力", "object": "杀人则变强，可全方位提升自身各方面能力"},
    {"subject": "李羽", "predicate": "与老人关系", "object": "爷孙；老人是搭伙过日子的感觉，不亲近但做好本分"},
    {"subject": "李羽", "predicate": "苏醒表现", "object": "沉默寡言，装作失忆，适应环境"},
    {"subject": "李羽家", "predicate": "田地", "object": "三四亩"},
    {"subject": "少爷", "predicate": "背景势力", "object": "地方豪强"},
    # 早期确认的世界观/主线（#214 摘要核对）
    {"subject": "小说", "predicate": "主线", "object": "王朝末年乱世，主角穿越受欺压，从一县之地起步统一国家终结乱世"},
    {"subject": "小说", "predicate": "修炼体系", "object": "采用《寂静杀戮》的命丛体系"},
    {"subject": "小说", "predicate": "外星人设定", "object": "偶然发现地球，因东方有修炼体系想研究；武力比东方顶尖高一阶段，被围攻会死，躲在西方搞科技"},
    {"subject": "小说", "predicate": "每章字数", "object": "按网文标准每章约3000字"},
]

if __name__ == "__main__":
    n = upsert_facts(STORY_FACTS)
    print(f"小说设定补录完成：{n} 条已写入 facts 永久层")
