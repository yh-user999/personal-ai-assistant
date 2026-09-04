"""小说章节全文索引与受限文件索引。"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from app.models.database import db_connection
from app.novel.file_store import NovelFileStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def rebuild_chapter_index(project_id: str) -> int:
    with db_connection() as conn:
        rows = conn.execute("SELECT chapter_no, title, content, draft_content FROM novel_chapters WHERE project_id=?", (project_id,)).fetchall()
        conn.execute("DELETE FROM novel_chapters_fts WHERE project_id=?", (project_id,))
        for row in rows:
            conn.execute("INSERT INTO novel_chapters_fts(project_id,chapter_no,title,content) VALUES(?,?,?,?)", (project_id, row["chapter_no"], row["title"], row["content"] or row["draft_content"]))
    return len(rows)


def search_chapters(project_id: str, query: str, *, limit: int = 50, offset: int = 0) -> list[dict]:
    query = query.strip()
    if not query:
        return []
    with db_connection() as conn:
        rows = conn.execute("SELECT project_id, chapter_no, title, snippet(novel_chapters_fts, 3, '<mark>', '</mark>', '…', 16) AS snippet FROM novel_chapters_fts WHERE novel_chapters_fts MATCH ? AND project_id=? LIMIT ? OFFSET ?", (query, project_id, limit, offset)).fetchall()
    return [dict(row) for row in rows]


def sync_file_index(project_id: str, root: str, *, rebuild: bool = False) -> dict:
    store = NovelFileStore(root)
    files: dict[str, tuple[int, int, str]] = {}
    for path in store.root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in store.ALLOWED_EXTENSIONS:
            continue
        rel = path.relative_to(store.root).as_posix()
        data = path.read_bytes()
        stat = path.stat()
        files[rel] = (stat.st_size, stat.st_mtime_ns, hashlib.sha256(data).hexdigest())
    added = updated = removed = 0
    with db_connection() as conn:
        if rebuild:
            conn.execute("DELETE FROM novel_file_index WHERE project_id=?", (project_id,))
        old = {row["relative_path"]: row for row in conn.execute("SELECT * FROM novel_file_index WHERE project_id=?", (project_id,)).fetchall()}
        for rel, (size, mtime, digest) in files.items():
            previous = old.get(rel)
            if previous is None:
                added += 1
            elif previous["size_bytes"] != size or previous["mtime_ns"] != mtime or previous["sha256"] != digest:
                updated += 1
            conn.execute("INSERT INTO novel_file_index(project_id,relative_path,size_bytes,mtime_ns,sha256,indexed_at) VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,relative_path) DO UPDATE SET size_bytes=excluded.size_bytes,mtime_ns=excluded.mtime_ns,sha256=excluded.sha256,indexed_at=excluded.indexed_at", (project_id, rel, size, mtime, digest, _now()))
        for rel in set(old) - set(files):
            conn.execute("DELETE FROM novel_file_index WHERE project_id=? AND relative_path=?", (project_id, rel))
            removed += 1
    return {"project_id": project_id, "files": len(files), "added": added, "updated": updated, "removed": removed, "consistent": removed == 0 and (added + updated) == 0}


def file_index_status(project_id: str) -> dict:
    with db_connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS count, MAX(indexed_at) AS indexed_at FROM novel_file_index WHERE project_id=?", (project_id,)).fetchone()
    return {"project_id": project_id, "files": row["count"], "indexed_at": row["indexed_at"]}
