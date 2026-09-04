"""大纲用例占位层，为后续文件事实源接入保留稳定接口。"""
from __future__ import annotations


class NovelOutlineService:
    def __init__(self, file_store=None) -> None:
        self.file_store = file_store

    def get_outline(self, project_id: str | None = None, chapter_no: str | None = None) -> str:
        if self.file_store is None:
            return ""
        relative = f"outline/chapter-{int(chapter_no):03d}.md" if chapter_no and chapter_no.isdigit() else "outline/README.md"
        return self.file_store.read_text(relative) if self.file_store.exists(relative) else ""
