"""可恢复的生成、审查、确认、发布状态机。"""
from __future__ import annotations

from dataclasses import dataclass

from app.novel.domain import GenerationJob, GenerationJobStatus


@dataclass
class NovelWorkflow:
    status: GenerationJobStatus = GenerationJobStatus.QUEUED
    repository: object | None = None
    job_id: str | None = None
    version: int = 1

    @classmethod
    def from_job(cls, job: GenerationJob, repository: object) -> NovelWorkflow:
        return cls(job.status, repository, job.job_id, job.version)

    def _set(self, status: GenerationJobStatus, **kwargs) -> None:
        if self.repository and self.job_id:
            job = self.repository.update_job(self.job_id, status, expected_version=self.version, **kwargs)
            self.version = job.version
        self.status = status

    def start_generation(self) -> None:
        self._set(GenerationJobStatus.GENERATING)

    def start_review(self) -> None:
        self._set(GenerationJobStatus.REVIEWING)

    def await_confirmation(self, *, review_result: dict | None = None) -> None:
        self._set(GenerationJobStatus.AWAITING_CONFIRMATION, review_result=review_result)

    def publish(self) -> None:
        if self.status is not GenerationJobStatus.AWAITING_CONFIRMATION:
            raise ValueError("只有等待确认的草稿才能发布")
        self._set(GenerationJobStatus.PUBLISHED)

    def cancel(self) -> None:
        if self.status is GenerationJobStatus.PUBLISHED:
            raise ValueError("已发布草稿不能取消")
        self._set(GenerationJobStatus.CANCELLED)

    def fail(self, error: str = "") -> None:
        self._set(GenerationJobStatus.FAILED, error=error)
