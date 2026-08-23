#!/usr/bin/env python3
"""生成 AI 分析包：把整个项目合并为单个 Markdown 文件（含目录树）。

用法: python scripts/build_ai_bundle.py [输出路径]
默认输出: /tmp/PROJECT_BUNDLE.md

产物特征：
- 单文件、纯文本，任何 AI 都能直接粘贴或上传
- 按 .gitignore 排除产物/密钥/缓存
- 文件头标注语言，AI 可正确解析代码块
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GITIGNORE = ROOT / ".gitignore"

# 额外排除（无论如何都不进分析包）
EXCLUDE_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", "dist", "build",
                "cache", "data", ".idea", ".vscode"}
EXCLUDE_FILES = {".DS_Store", "Thumbs.db"}
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".db", ".sqlite", ".exe", ".zip",
              ".pyc", ".whl", ".tgz", ".woff", ".woff2", ".ttf"}

LANG_MAP = {
    ".py": "python", ".md": "markdown", ".json": "json", ".yaml": "yaml",
    ".yml": "yaml", ".toml": "toml", ".html": "html", ".css": "css",
    ".js": "javascript", ".sh": "bash", ".ps1": "powershell", ".txt": "text",
}


def load_gitignore() -> set[str]:
    patterns = set()
    if GITIGNORE.exists():
        for line in GITIGNORE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.add(line.rstrip("/"))
    return patterns


def should_skip(path: Path, rel: Path, ignore: set[str]) -> bool:
    parts = rel.parts
    if any(p in EXCLUDE_DIRS for p in parts):
        return True
    if rel.name in EXCLUDE_FILES:
        return True
    if rel.suffix in BINARY_EXT:
        return True
    for pat in ignore:
        if pat in parts:
            return True
    return False


def build_tree(ignore: set[str]) -> str:
    lines = []
    for p in sorted(ROOT.rglob("*")):
        rel = p.relative_to(ROOT)
        if should_skip(p, rel, ignore):
            continue
        depth = len(rel.parts) - 1
        prefix = "  " * depth + ("└── " if depth else "")
        if p.is_dir():
            lines.append(f"{prefix}{rel.name}/")
        else:
            try:
                n = sum(1 for _ in p.open(encoding="utf-8", errors="ignore"))
            except OSError:
                n = 0
            lines.append(f"{prefix}{rel.name}  ({n} 行)")
    return "\n".join(lines)


def collect_files(ignore: set[str]) -> list[tuple[Path, str]]:
    files = []
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if should_skip(p, rel, ignore):
            continue
        files.append((p, str(rel)))
    return files


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/PROJECT_BUNDLE.md")
    ignore = load_gitignore()

    # 项目元信息
    meta = {}
    pyproject = ROOT / "pyproject.toml"
    if pyproject.exists():
        meta["pyproject"] = pyproject.read_text(encoding="utf-8")[:500]

    tree = build_tree(ignore)
    files = collect_files(ignore)
    total_lines = 0
    for p, _ in files:
        try:
            total_lines += sum(1 for _ in p.open(encoding="utf-8", errors="ignore"))
        except OSError:
            pass

    chunks = []
    chunks.append("# Project Bundle — Personal AI Assistant\n")
    chunks.append(f"> 生成时间: {datetime.now(timezone.utc).isoformat()}\n")
    chunks.append("> 用途: 供 AI 分析。包含项目结构、全部源码与文档。\n")
    chunks.append(f"> 统计: {len(files)} 个文本文件, 约 {total_lines} 行代码\n")
    chunks.append("---\n")
    chunks.append("## 项目定位\n")
    chunks.append("个人智能助手：Windows 本地 + JD 服务器混合部署。桌面悬浮机器人形态，"
                  "实现「记忆 → 分析 → 学习」闭环。包含 FastAPI 服务端（记忆/统计/周报）、"
                  "Windows 行为采集器、PySide6 桌面悬浮球。\n")
    chunks.append("---\n")
    chunks.append("## 目录树\n```\n" + tree + "\n```\n")
    chunks.append("---\n")

    for p, rel in files:
        try:
            content = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        lang = LANG_MAP.get(p.suffix, "")
        chunks.append(f"## 文件: {rel}\n")
        if content.strip():
            chunks.append(f"```{lang}\n{content}\n```\n")
        chunks.append("---\n")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(chunks), encoding="utf-8")
    print(f"生成完成: {out} ({out.stat().st_size / 1024:.1f} KB, {len(files)} 文件)")


if __name__ == "__main__":
    main()
