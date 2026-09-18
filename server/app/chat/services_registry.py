"""ChatRuntime.services 的模块注册表：单一清单 + 按需覆盖。

api/_build_runtime 从这里组装（原 36 模块 SimpleNamespace 手工罗列收口于此）。
存的是**模块对象**而非实例：monkeypatch 服务模块属性实时生效的既有契约不变，
模块级缓存/ContextVar 语义也不变。测试覆盖个别服务用 ``make_services(overrides)``。

模块路径：拥有独立领域包的模块（fitness/novel/group）在 ``DOMAIN_MODULE_PATHS``
登记完整路径；其余仍约定 ``app.services.<name>``。新增领域模块时两处都要动。
"""
from __future__ import annotations

import importlib
from types import SimpleNamespace

from app.novel import NovelApplicationService

# 具备独立领域包的模块：显式登记完整路径（其余仍从 app/services 加载）。
DOMAIN_MODULE_PATHS = {
    # AI 资讯日报
    "ai_news": "app.ai_news.service",
    # 健身
    "fitness": "app.fitness.service",
    "fitness_catalog": "app.fitness.catalog",
    "fitness_coach": "app.fitness.coach",
    "fitness_nutrition": "app.fitness.nutrition",
    "fitness_training": "app.fitness.training",
    # 小说
    "novel_entities": "app.novel.entities",
    "novel_lexicon": "app.novel.lexicon",
    "novel_writing": "app.novel.writing",
    "chapter_analysis": "app.novel.chapter_analysis",
    # 群聊
    "group_care": "app.group.care",
    "group_expression": "app.group.expression",
    "group_relationship": "app.group.relationship",
}

# 新增 service：模块放进 app/services/ 后在此登记一行即可。
SERVICE_MODULES = (
    "ai_news",
    "behavior_context",
    "chapter_analysis",
    "cooccurrence",
    "confirm",
    "concern_tracker",
    "documents",
    "executor",
    "fact_extract",
    "fitness",
    "fitness_catalog",
    "fitness_coach",
    "fitness_nutrition",
    "fitness_training",
    "few_shot",
    "goals",
    "group_care",
    "group_expression",
    "group_relationship",
    "growth",
    "identity_guard",
    "index_healer",
    "initiative",
    "intent_goals",
    "jargon",
    "knowledge_domain",
    "knowledge_hint",
    "message_search",
    "mood",
    "novel_entities",
    "novel_writing",
    "plain_text",
    "profile",
    "reminders",
    "request_trace",
    "resume",
    "sanitize",
    "self_reflect",
    "self_state",
    "robot_state",
    "slang",
    "subjective_time",
    "unresolved",
    "worklog",
)


def _module_path(name: str) -> str:
    """领域包模块走显式路径，其余约定为 app/services/<name>。"""
    return DOMAIN_MODULE_PATHS.get(name, f"app.services.{name}")


def make_services(overrides: dict | None = None) -> SimpleNamespace:
    """按清单 import 全部服务模块并组装命名空间；overrides 优先（测试用）。"""
    mods = {name: importlib.import_module(_module_path(name)) for name in SERVICE_MODULES}
    mods["novel"] = NovelApplicationService.from_legacy(
        mods["novel_writing"], mods["chapter_analysis"], mods["novel_entities"]
    )
    if overrides:
        mods.update(overrides)
    return SimpleNamespace(**mods)
