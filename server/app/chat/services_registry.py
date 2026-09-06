"""ChatRuntime.services 的模块注册表：单一清单 + 按需覆盖。

api/_build_runtime 从这里组装（原 36 模块 SimpleNamespace 手工罗列收口于此）。
存的是**模块对象**而非实例：monkeypatch 服务模块属性实时生效的既有契约不变，
模块级缓存/ContextVar 语义也不变。测试覆盖个别服务用 ``make_services(overrides)``。
"""
from __future__ import annotations

import importlib
from types import SimpleNamespace

from app.novel import NovelApplicationService

# 新增 service：模块放进 app/services/ 后在此登记一行即可。
SERVICE_MODULES = (
    "behavior_context",
    "chapter_analysis",
    "cooccurrence",
    "confirm",
    "concern_tracker",
    "documents",
    "executor",
    "fact_extract",
    "fitness",
    "few_shot",
    "goals",
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
    "slang",
    "subjective_time",
    "unresolved",
    "worklog",
)


def make_services(overrides: dict | None = None) -> SimpleNamespace:
    """按清单 import 全部服务模块并组装命名空间；overrides 优先（测试用）。"""
    mods = {name: importlib.import_module(f"app.services.{name}") for name in SERVICE_MODULES}
    mods["novel"] = NovelApplicationService.from_legacy(
        mods["novel_writing"], mods["chapter_analysis"], mods["novel_entities"]
    )
    if overrides:
        mods.update(overrides)
    return SimpleNamespace(**mods)
