"""小说审查用例适配层。"""
from __future__ import annotations

from app.novel.domain import ReviewReport


class NovelReviewService:
    def __init__(self, chapter_service, writer_service) -> None:
        self.chapter_service = chapter_service
        self.writer_service = writer_service

    async def review_chapter(self, text: str, *, user_id: str | None = None) -> ReviewReport:
        result = await self.chapter_service.analyze_chapter(text, user_id=user_id)
        return ReviewReport(ok="发现" not in result["reply"], reply=result["reply"])

    async def check_conflicts(self, text: str) -> ReviewReport:
        result = await self.writer_service.check_conflicts(text)
        return ReviewReport(ok="未发现" in result["reply"], reply=result["reply"])
