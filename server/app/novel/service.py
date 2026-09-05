"""小说应用服务：统一编排入口，暂时复用既有模块实现。"""
from __future__ import annotations

from dataclasses import dataclass

from app.novel.context import NovelContextBuilder, NovelProjectContext
from app.novel.generation import NovelGenerationService
from app.novel.outline import NovelOutlineService
from app.novel.repository import SQLiteNovelRepository
from app.novel.review import NovelReviewService
from app.novel.workflow import NovelWorkflow


@dataclass(frozen=True)
class NovelApplicationService:
    writer: object
    chapters: object
    entities: object | None = None
    context_builder: NovelContextBuilder | None = None
    generation: NovelGenerationService | None = None
    review: NovelReviewService | None = None
    outline: NovelOutlineService | None = None
    repository: SQLiteNovelRepository | None = None

    @classmethod
    def from_legacy(cls, writer, chapters, entities=None) -> NovelApplicationService:
        return cls(
            writer=writer,
            chapters=chapters,
            entities=entities,
            context_builder=NovelContextBuilder(chapter_service=chapters),
            generation=NovelGenerationService(writer),
            review=NovelReviewService(chapters, writer),
            outline=NovelOutlineService(),
            repository=SQLiteNovelRepository(),
        )

    def build_context(self, project_id: str | None = None, **kwargs) -> NovelProjectContext:
        builder = self.context_builder or NovelContextBuilder(chapter_service=self.chapters)
        return builder.build(project_id, **kwargs)

    def parse_writing_log(self, message: str):
        return self.writer.parse_writing_log(message)

    def add_writing_log(self, chapter: str | None, words: int):
        return self.writer.add_writing_log(chapter, words)

    def writing_summary(self) -> str:
        return self.writer.writing_summary()

    def parse_conflict_command(self, message: str):
        return self.writer.parse_conflict_command(message)

    def looks_like_file_path(self, text: str) -> bool:
        return self.writer.looks_like_file_path(text)

    async def review_conflicts(self, text: str):
        return await (self.review or NovelReviewService(self.chapters, self.writer)).check_conflicts(text)

    def parse_analysis_command(self, message: str):
        return self.chapters.parse_analysis_command(message)

    async def review_chapter(self, text: str, *, user_id: str | None = None):
        return await (self.review or NovelReviewService(self.chapters, self.writer)).review_chapter(text, user_id=user_id)

    def parse_archive_command(self, message: str):
        return self.chapters.parse_archive_command(message)

    def archive_chapter(self, chapter: str, summary: str, threads: list[str], *, source: str = "manual") -> None:
        self.chapters.upsert_chapter_note(chapter, summary, threads, source=source)

    def parse_continue_command(self, message: str):
        return self.writer.parse_continue_command(message)

    async def draft_chapter(self, text: str, *, project_id: str | None = None):
        return await (self.generation or NovelGenerationService(self.writer)).continue_story(text, project_id=project_id)

    def new_workflow(self) -> NovelWorkflow:
        return NovelWorkflow()

    def project_repository(self) -> SQLiteNovelRepository:
        return self.repository or SQLiteNovelRepository()

    def create_project(self, name: str, **kwargs):
        return self.project_repository().create_project(name, **kwargs)

    def list_projects(self, user_id: str = "owner"):
        return self.project_repository().list_projects(user_id)

    def create_generation_job(self, project_id: str, chapter_no: str, idempotency_key: str, prompt: str = ""):
        return self.project_repository().create_job(project_id, chapter_no, idempotency_key, prompt)
