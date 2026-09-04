"""按项目组装小说写作上下文，并控制预算。"""
from __future__ import annotations

from dataclasses import dataclass

from app.novel.domain import NovelProject
from app.novel.repository import LegacyNovelRepository, NovelRepository


@dataclass(frozen=True)
class NovelProjectContext:
    project: NovelProject
    continuity: str = ""
    authority: str = ""
    outline: str = ""

    def render(self, max_chars: int = 5000) -> str:
        parts = [part for part in (self.authority, self.continuity, self.outline) if part]
        return "\n\n".join(parts)[:max_chars]


class NovelContextBuilder:
    def __init__(self, repository: NovelRepository | None = None, chapter_service=None) -> None:
        self.repository = repository or LegacyNovelRepository()
        self.chapter_service = chapter_service

    def build(self, project_id: str | None = None, *, authority: str = "", outline: str = "") -> NovelProjectContext:
        continuity = ""
        if self.chapter_service is not None:
            continuity = self.chapter_service.build_continuity_block()
        return NovelProjectContext(
            project=self.repository.get_project(project_id),
            continuity=continuity,
            authority=authority,
            outline=outline,
        )
