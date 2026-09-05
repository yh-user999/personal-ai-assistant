"""小说审查用例适配层。"""
from __future__ import annotations

from app.novel.domain import ReviewReport


class NovelReviewService:
    def __init__(self, chapter_service, writer_service) -> None:
        self.chapter_service = chapter_service
        self.writer_service = writer_service

    async def review_chapter(
        self,
        text: str,
        *,
        user_id: str | None = None,
        request_id: str | None = None,
    ) -> ReviewReport:
        kwargs = {}
        if user_id:
            kwargs["user_id"] = user_id
        if request_id:
            kwargs["request_id"] = request_id
        result = await self.chapter_service.analyze_chapter(text, **kwargs)
        return ReviewReport(ok="发现" not in result["reply"], reply=result["reply"])

    async def check_conflicts(
        self,
        text: str,
        *,
        user_id: str | None = None,
        request_id: str | None = None,
    ) -> ReviewReport:
        kwargs = {}
        if user_id:
            kwargs["user_id"] = user_id
        if request_id:
            kwargs["request_id"] = request_id
        result = await self.writer_service.check_conflicts(text, **kwargs)
        return ReviewReport(ok="未发现" in result["reply"], reply=result["reply"])
