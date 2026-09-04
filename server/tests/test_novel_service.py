"""小说应用服务第一阶段契约测试。"""
import asyncio

from app.novel.domain import GenerationJob, GenerationJobStatus, job_payload
from app.novel.file_store import NovelFileStore
from app.novel.service import NovelApplicationService
from app.novel.workflow import NovelWorkflow


class FakeWriter:
    def parse_writing_log(self, message):
        return ("1", 100) if message == "ok" else None

    def add_writing_log(self, chapter, words):
        return 7

    def writing_summary(self):
        return "summary"

    def parse_conflict_command(self, message):
        return message.removeprefix("冲突：") if message.startswith("冲突：") else None

    def looks_like_file_path(self, text):
        return "/" in text

    async def check_conflicts(self, text):
        return {"reply": "✅ 未发现"}

    def parse_continue_command(self, message):
        return message.removeprefix("续写：") if message.startswith("续写：") else None

    async def continue_story(self, text):
        return "draft:" + text


class FakeChapters:
    def parse_analysis_command(self, message):
        return None

    async def analyze_chapter(self, text, user_id=None):
        return {"reply": "reviewed"}

    def parse_archive_command(self, message):
        return None

    def upsert_chapter_note(self, *args, **kwargs):
        self.archived = args

    def build_continuity_block(self):
        return "continuity"


def test_generation_job_payload_has_stable_json_fields():
    job = GenerationJob("j", "idem", "p", "1", GenerationJobStatus.QUEUED, prompt="写作提示")
    payload = job_payload(job)
    assert payload["status"] == "queued"
    assert payload["prompt"] == "写作提示"
    assert payload["attempts"] == 0
    assert set(payload) == {"job_id", "idempotency_key", "project_id", "chapter_no", "status", "prompt", "draft_content", "review_result", "error", "attempts", "version"}


def test_application_service_wraps_legacy_use_cases():
    service = NovelApplicationService.from_legacy(FakeWriter(), FakeChapters())
    assert service.parse_writing_log("ok") == ("1", 100)
    assert service.writing_summary() == "summary"
    assert asyncio.run(service.draft_chapter("片段")).text == "draft:片段"
    assert asyncio.run(service.review_conflicts("内容")).reply == "✅ 未发现"
    assert service.build_context().continuity == "continuity"


def test_workflow_requires_confirmation_before_publish():
    workflow = NovelWorkflow()
    assert workflow.status is GenerationJobStatus.QUEUED
    workflow.start_generation()
    workflow.start_review()
    workflow.await_confirmation()
    workflow.publish()
    assert workflow.status is GenerationJobStatus.PUBLISHED


def test_file_store_writes_numbered_chapter(tmp_path):
    store = NovelFileStore(tmp_path)
    assert store.write_chapter("2", "正文", title="第二章") == "chapters/chapter-002.md"
    assert store.read_text("chapters/chapter-002.md") == "# 第二章\n\n正文"


def test_file_store_rejects_escape_and_writes_atomically(tmp_path):
    store = NovelFileStore(tmp_path)
    store.write_text("chapters/001.md", "正文")
    assert store.read_text("chapters/001.md") == "正文"
    try:
        store.read_text("../outside.md")
    except ValueError as exc:
        assert "NOVEL_ROOT" in str(exc)
    else:
        raise AssertionError("path escape should be rejected")
