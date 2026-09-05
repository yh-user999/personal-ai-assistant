"""小说领域模型与工作流状态。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class GenerationJobStatus(StrEnum):
    QUEUED = "queued"
    GENERATING = "generating"
    REVIEWING = "reviewing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"


_ALLOWED_JOB_TRANSITIONS: dict[GenerationJobStatus, frozenset[GenerationJobStatus]] = {
    GenerationJobStatus.QUEUED: frozenset({GenerationJobStatus.GENERATING, GenerationJobStatus.AWAITING_CONFIRMATION, GenerationJobStatus.CANCELLED}),
    GenerationJobStatus.GENERATING: frozenset({GenerationJobStatus.REVIEWING, GenerationJobStatus.AWAITING_CONFIRMATION, GenerationJobStatus.FAILED, GenerationJobStatus.CANCELLED, GenerationJobStatus.QUEUED}),
    GenerationJobStatus.REVIEWING: frozenset({GenerationJobStatus.AWAITING_CONFIRMATION, GenerationJobStatus.FAILED, GenerationJobStatus.CANCELLED}),
    GenerationJobStatus.AWAITING_CONFIRMATION: frozenset({GenerationJobStatus.PUBLISHED, GenerationJobStatus.FAILED, GenerationJobStatus.CANCELLED}),
    GenerationJobStatus.FAILED: frozenset({GenerationJobStatus.QUEUED, GenerationJobStatus.CANCELLED}),
    GenerationJobStatus.CANCELLED: frozenset({GenerationJobStatus.QUEUED}),
    GenerationJobStatus.PUBLISHED: frozenset(),
}


def can_transition_job(current: GenerationJobStatus, target: GenerationJobStatus) -> bool:
    return current == target or target in _ALLOWED_JOB_TRANSITIONS[current]


@dataclass(frozen=True)
class NovelProject:
    project_id: str
    name: str
    slug: str
    owner_id: str = "owner"
    root: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    updated_at: str | None = None


@dataclass(frozen=True)
class NovelDraft:
    project_id: str
    chapter_no: str
    content: str
    version: int = 1
    status: str = "draft"


@dataclass(frozen=True)
class GenerationJob:
    job_id: str
    idempotency_key: str
    project_id: str
    chapter_no: str
    status: GenerationJobStatus
    prompt: str = ""
    draft_content: str = ""
    review_result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    attempts: int = 0
    progress: int = 0
    heartbeat_at: str | None = None
    version: int = 1


@dataclass(frozen=True)
class Chapter:
    chapter_no: str
    title: str = ""
    content: str = ""
    project_id: str = ""
    status: str = "draft"
    version: int = 1


@dataclass(frozen=True)
class ReviewReport:
    ok: bool
    reply: str
    problems: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class DraftResult:
    text: str
    project_id: str = ""
    chapter_no: str | None = None
    word_count: int = 0


def project_payload(value: NovelProject) -> dict[str, Any]:
    return {
        "project_id": value.project_id,
        "name": value.name,
        "slug": value.slug,
        "owner_id": value.owner_id,
        "root": value.root,
        "metadata": dict(value.metadata),
        "version": value.version,
        "updated_at": value.updated_at,
    }


def chapter_payload(value: Chapter) -> dict[str, Any]:
    return {
        "chapter_no": value.chapter_no,
        "title": value.title,
        "content": value.content,
        "project_id": value.project_id,
        "status": value.status,
        "version": value.version,
    }


def draft_payload(value: NovelDraft) -> dict[str, Any]:
    return {
        "project_id": value.project_id,
        "chapter_no": value.chapter_no,
        "content": value.content,
        "version": value.version,
        "status": value.status,
    }


def job_payload(value: GenerationJob, *, include_runtime: bool = False) -> dict[str, Any]:
    payload = {
        "job_id": value.job_id,
        "idempotency_key": value.idempotency_key,
        "project_id": value.project_id,
        "chapter_no": value.chapter_no,
        "status": value.status.value,
        "prompt": value.prompt,
        "draft_content": value.draft_content,
        "review_result": dict(value.review_result),
        "error": value.error,
        "attempts": value.attempts,
        "version": value.version,
    }
    if include_runtime or value.progress or value.heartbeat_at:
        payload.update({"progress": value.progress, "heartbeat_at": value.heartbeat_at})
    return payload
