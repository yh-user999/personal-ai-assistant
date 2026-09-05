"""小说生成用例的纯生成适配层。"""
from __future__ import annotations

from app.novel.domain import DraftResult


class NovelGenerationService:
    def __init__(self, writer_service) -> None:
        self.writer_service = writer_service

    async def continue_story(
        self,
        text: str,
        *,
        project_id: str | None = None,
        user_id: str | None = None,
        request_id: str | None = None,
    ) -> DraftResult:
        kwargs = {}
        if user_id:
            kwargs["user_id"] = user_id
        if request_id:
            kwargs["request_id"] = request_id
        result = await self.writer_service.continue_story(text, **kwargs)
        return DraftResult(
            text=result,
            project_id=project_id or "default",
            word_count=len(result.replace(" ", "")),
        )

    async def check_conflicts(
        self,
        text: str,
        *,
        user_id: str | None = None,
        request_id: str | None = None,
    ):
        kwargs = {}
        if user_id:
            kwargs["user_id"] = user_id
        if request_id:
            kwargs["request_id"] = request_id
        return await self.writer_service.check_conflicts(text, **kwargs)
