"""小说入库脚本（可复用）：大块切分 + 分批向量化 + 跳过脱敏。

用法（在服务器上，server venv 环境）：
    .venv/bin/python scripts/ingest_novel.py <小说文件路径> [文档名] [块大小] [重叠]

默认：文档名取文件名去扩展名、块大小 1500 字、重叠 150 字（一场戏一块）。
入库前自动删除同名文档旧块（replace），重复执行 = 重新入库，不产生重复。
"""
import asyncio
import logging
import sys
import time
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER_ROOT))

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
logger = logging.getLogger("ingest-novel")

from app.core import knowledge  # noqa: E402


def _read(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8", "gbk", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


async def main() -> None:
    args = sys.argv[1:]
    if not args:
        print("用法：ingest_novel.py <文件路径> [文档名] [块大小=1500] [重叠=150]")
        sys.exit(1)
    path = Path(args[0])
    doc_name = args[1] if len(args) > 1 else path.stem
    chunk_size = int(args[2]) if len(args) > 2 else 1500
    overlap = int(args[3]) if len(args) > 3 else 150

    t0 = time.time()
    text = _read(path)
    logger.info("文件读取完成：%s（%d 字符），块大小 %d/重叠 %d",
                path.name, len(text), chunk_size, overlap)
    result = await knowledge.ingest_document(
        doc_name, text, replace=True, sanitize_content=False,
        chunk_size=chunk_size, overlap=overlap,
    )
    logger.info("入库完成：%s，总用时 %.0f 秒", result, time.time() - t0)


if __name__ == "__main__":
    asyncio.run(main())
