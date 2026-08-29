"""健身减脂种子数据（第 6.29 课）：用户身体数据入 facts + 健身知识卡入库。

用法（服务器上）：cd server && .venv/bin/python scripts/seed_fitness.py
幂等：facts 按 (subject, predicate) upsert；知识卡按 keywords 查重。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DB_PATH", "./data/assistant.db")

from app.models.database import init_db  # noqa: E402
from app.services import fitness  # noqa: E402
from app.services.fact_extract import upsert_facts  # noqa: E402


def main() -> None:
    init_db()

    # ① 用户身体数据（用户 2026-08-29 亲口确认）
    n = upsert_facts([
        {"subject": "用户", "predicate": "身高为", "object": "170cm"},
        {"subject": "用户", "predicate": "体重为", "object": "70公斤（2026-08-29 起始记录）"},
        {"subject": "用户", "predicate": "年龄", "object": "28岁"},
        {"subject": "用户", "predicate": "性别", "object": "男"},
        {"subject": "用户", "predicate": "健身基础", "object": "练过一年半，近一个月停练"},
        {"subject": "用户", "predicate": "健身目标", "object": "减脂（BMI 24.2 略超正常上限，目标体重待用户确认）"},
    ])
    print(f"身体数据 facts 写入/更新：{n} 条")

    # ② 健身知识卡
    added = fitness.seed_fitness_cards()
    print(f"健身知识卡新增：{added} 张")

    conn_ok = fitness.get_fitness_facts("平台期")
    print(f"验证检索「平台期」命中：{len(conn_ok)} 张")


if __name__ == "__main__":
    main()
