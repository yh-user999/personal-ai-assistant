"""项目文档同步：把仓库 docs/*.md 全部喂进机器人知识库。

用法（服务器上）：
    cd server && .venv/bin/python scripts/sync_docs_to_knowledge.py

效果：机器人可通过知识库回答项目进展问题（"进行到第几课了"等）。
每次文档更新后重跑即可（ingest 按 doc_name 覆盖，不产生重复）。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import knowledge  # noqa: E402

DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"


async def main() -> None:
    docs = sorted(DOCS_DIR.glob("*.md"))
    if not docs:
        print(f"未找到文档: {DOCS_DIR}")
        return
    total_chunks = 0
    for md in docs:
        content = md.read_text(encoding="utf-8")
        result = await knowledge.ingest_document(md.stem, content)
        chunks = result.get("chunks", 0)
        total_chunks += chunks
        print(f"  {md.name}: {chunks} 块")
    print(f"同步完成：{len(docs)} 份文档，共 {total_chunks} 块")


if __name__ == "__main__":
    asyncio.run(main())
