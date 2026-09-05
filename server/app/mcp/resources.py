"""私人助手 MCP Resources：只读、固定 URI、复用业务服务。"""
from __future__ import annotations

import json
from typing import Any

from app.core import memory
from app.novel.repository import SQLiteNovelRepository
from app.services import daily_summary, goals, profile, unresolved

from .context import current_context
from .permissions import require_owner
from .schemas import cap_payload


def _json(value: Any) -> str:
    return json.dumps(cap_payload(value), ensure_ascii=False, separators=(",", ":"), default=str)


def _identity():
    return require_owner(current_context())


def get_profile_resource() -> str:
    identity = _identity()
    return _json({
        "user_id": identity.uid,
        "profile_text": profile.get_profile_injection(user_id=identity.uid),
    })


def get_facts_resource() -> str:
    identity = _identity()
    return _json({
        "user_id": identity.uid,
        "facts_text": memory.get_facts_injection(user_id=identity.uid),
    })


def get_goals_resource() -> str:
    identity = _identity()
    return _json({"results": goals.list_goals(identity.uid, limit=20)})


def get_open_issues_resource() -> str:
    identity = _identity()
    return _json({"results": unresolved.list_open_issues(identity.uid, limit=20)})


def get_daily_summary_resource() -> str:
    identity = _identity()
    return _json({"summary": daily_summary.get_latest_daily_summary(user_id=identity.uid)})


def get_novel_projects_resource() -> str:
    identity = _identity()
    repo = SQLiteNovelRepository(owner_id=identity.uid)
    return _json({"projects": [p.__dict__ for p in repo.list_projects(identity.uid)]})


def register_resources(server: Any) -> None:
    server.resource(
        "assistant://profile",
        name="assistant-profile",
        description="当前主人的结构化画像摘要",
        mime_type="application/json",
    )(get_profile_resource)
    server.resource(
        "assistant://facts",
        name="assistant-facts",
        description="当前主人的持久事实",
        mime_type="application/json",
    )(get_facts_resource)
    server.resource(
        "assistant://goals",
        name="assistant-goals",
        description="当前主人的目标列表",
        mime_type="application/json",
    )(get_goals_resource)
    server.resource(
        "assistant://open-issues",
        name="assistant-open-issues",
        description="当前主人的未解决问题",
        mime_type="application/json",
    )(get_open_issues_resource)
    server.resource(
        "assistant://daily-summary",
        name="assistant-daily-summary",
        description="最近一份已生成的每日小结",
        mime_type="application/json",
    )(get_daily_summary_resource)
    server.resource(
        "assistant://novel/projects",
        name="assistant-novel-projects",
        description="当前身份可访问的小说项目",
        mime_type="application/json",
    )(get_novel_projects_resource)


__all__ = ["register_resources"]
