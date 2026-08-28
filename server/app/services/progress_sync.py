"""项目进度同步服务：把仓库文档与课程进度"灌"进机器人（修复流程缺环）。

背景：8-26 后项目更新只进 GitHub 文档，没人重跑同步 → 机器人的知识库
facts 停在旧状态（第11课显示"待开始"）。本服务由 scheduler 每早 4:10
自动执行，也可手动调用。两个动作：
1. sync_docs_to_knowledge：docs/*.md 全部重灌知识库（按 doc_name 覆盖）
2. refresh_progress_facts：课程/项目进度事实刷新（个人小说类事实不动）
"""
import asyncio
import logging
from pathlib import Path

from app.core import knowledge
from app.models.database import connect

logger = logging.getLogger("assistant.progress_sync")

DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"

# 与 docs/LEARNING_PROGRESS.md 同步维护的进度快照（subject, predicate, object）
PROGRESS_FACTS = [
    ("六课带教计划", "状态", "第0-5课全部完成"),
    ("第6课", "状态", "测试工程与CI待开始"),
    ("第7课", "状态", "行为数据仪表盘待开始"),
    ("第8课", "状态", "QQ私聊接入待开始"),
    ("第9课", "状态", "RAG知识库已完成"),
    ("第10课", "状态", "检索评测已完成"),
    ("第11课", "状态", "执行器通道已完成"),
    ("第12课", "状态", "Goal系统与unresolved追踪已完成"),
    ("第13课", "状态", "执行器扩展（文件手+脚本脚）已完成"),
    ("第14课", "状态", "动画形象与皮肤主题系统已完成（v4.8）"),
    ("项目", "知识库", "已入库两本小说并建设定卡（RAG+别名融合）"),
    ("项目", "当前版本", "v4.8 主题系统（4套配色）"),
]


def refresh_progress_facts() -> int:
    """课程/项目进度事实刷新（幂等：先清理课程/项目类，再插入快照）。"""
    conn = connect()
    try:
        conn.execute(
            "DELETE FROM facts WHERE subject LIKE '第%课' OR subject='六课带教计划' OR subject='项目'"
        )
        for sub, pred, obj in PROGRESS_FACTS:
            conn.execute(
                "INSERT INTO facts (subject, predicate, object, confidence, updated_at) "
                "VALUES (?, ?, ?, 0.9, ?)",
                (sub, pred, obj, "2026-08-28T00:00:00+00:00"),
            )
        conn.commit()
    finally:
        conn.close()
    return len(PROGRESS_FACTS)


async def sync_docs_to_knowledge() -> int:
    """docs/*.md 重灌知识库（ingest replace 语义，无重复）。"""
    docs = sorted(DOCS_DIR.glob("*.md"))
    total = 0
    for md in docs:
        result = await knowledge.ingest_document(md.stem, md.read_text(encoding="utf-8"))
        total += result.get("chunks", 0)
        logger.info("文档同步: %s → %d 块", md.name, result.get("chunks", 0))
    return total


async def sync_progress_to_bot() -> dict:
    """一键同步（scheduler 每日调用）。"""
    n_facts = refresh_progress_facts()
    n_chunks = await sync_docs_to_knowledge()
    logger.info("进度同步完成：facts %d 条，文档 %d 块", n_facts, n_chunks)
    return {"facts": n_facts, "chunks": n_chunks}


def main() -> None:
    """手动运行：cd server && .venv/bin/python -m app.services.progress_sync"""
    async def _run() -> None:
        result = await sync_progress_to_bot()
        print(f"同步完成：facts {result['facts']} 条，文档 {result['chunks']} 块")

    asyncio.run(_run())


if __name__ == "__main__":
    main()