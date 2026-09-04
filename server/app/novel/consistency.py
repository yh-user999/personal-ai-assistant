"""小说数据库与项目文件的一致性检查。"""
from __future__ import annotations

from app.novel.file_store import NovelFileStore
from app.novel.repository import SQLiteNovelRepository


def check_project(project_id: str, *, repository: SQLiteNovelRepository | None = None) -> dict:
    repo = repository or SQLiteNovelRepository()
    project = repo.get_project(project_id)
    issues: list[dict] = []
    checked = 0
    if not project.root:
        return {"project_id": project_id, "checked": 0, "issues": [{"kind": "missing_root", "repair": "配置项目 root"}]}
    store = NovelFileStore(project.root)
    for chapter in repo.list_chapters(project_id):
        if chapter.status != "published":
            continue
        checked += 1
        path = f"chapters/chapter-{int(chapter.chapter_no):03d}.md" if chapter.chapter_no.isdigit() else ""
        if not path or not store.exists(path):
            issues.append({"chapter_no": chapter.chapter_no, "kind": "missing_file", "repair": "调用 file-sync 接口"})
            continue
        expected = f"# {chapter.title}\n\n{chapter.content}" if chapter.title else chapter.content
        if store.read_text(path) != expected:
            issues.append({"chapter_no": chapter.chapter_no, "kind": "content_mismatch", "repair": "调用 file-sync 接口"})
    return {"project_id": project_id, "checked": checked, "ok": not issues, "issues": issues}
