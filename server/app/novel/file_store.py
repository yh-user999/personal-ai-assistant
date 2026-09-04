"""受限小说文件存储。"""
from __future__ import annotations

import os
from pathlib import Path


class NovelFileStore:
    ALLOWED_EXTENSIONS = {".md", ".txt", ".json"}

    def __init__(self, root: str | os.PathLike[str], *, max_bytes: int = 2_000_000) -> None:
        self.root = Path(root).expanduser().resolve()
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def _safe_path(self, relative_path: str) -> Path:
        raw = Path(relative_path)
        if raw.is_absolute() or ".." in raw.parts:
            raise ValueError("小说文件路径必须是 NOVEL_ROOT 内的相对路径")
        if raw.suffix.lower() not in self.ALLOWED_EXTENSIONS:
            raise ValueError("不支持的小说文件扩展名")
        candidate = (self.root / raw).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("小说文件路径超出 NOVEL_ROOT")
        return candidate

    def read_text(self, relative_path: str) -> str:
        return self._safe_path(relative_path).read_text(encoding="utf-8")

    def write_text(self, relative_path: str, content: str) -> None:
        if len(content.encode("utf-8")) > self.max_bytes:
            raise ValueError("小说文件超过大小限制")
        path = self._safe_path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, path)

    def exists(self, relative_path: str) -> bool:
        return self._safe_path(relative_path).exists()

    def write_chapter(self, chapter_no: str, content: str, *, title: str = "") -> str:
        """按固定命名写入章节，避免调用方拼接任意文件路径。"""
        safe_no = str(chapter_no).strip()
        if not safe_no.isdigit():
            raise ValueError("章节号必须是数字")
        relative = f"chapters/chapter-{int(safe_no):03d}.md"
        body = f"# {title.strip()}\n\n{content}" if title.strip() else content
        self.write_text(relative, body)
        return relative
