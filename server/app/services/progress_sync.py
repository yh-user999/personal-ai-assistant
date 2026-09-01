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

DOCS_DIR = Path(__file__).resolve().parents[3] / "docs"  # 仓库根 /docs

# 与 docs/LEARNING_PROGRESS.md 同步维护的进度快照。
# v0.4.1：课程进度聚合成单条事实——14 条"第X课 状态"塞进 prompt 既占字数
# 又和画像重复（模型两头读、口径打架，回复显得啰嗦且画像混乱）；
# 聚合后 facts 更短、口径单一。内容按当前真实进度维护（第8课已上线）。
PROGRESS_FACTS = [
    ("六课带教计划", "状态", "第0-5课全部完成"),
    ("扩展课程进度", "状态",
     "第6课测试工程与CI待开始；第7课行为数据仪表盘待开始；"
     "第8课QQ私聊接入已完成（含v0.4多人支持）；第9课RAG知识库已完成；"
     "第10课检索评测已完成；第11课执行器通道已完成；"
     "第12课Goal系统与unresolved已完成；第13课执行器扩展已完成；"
     "第14课动画形象与主题系统已完成"),
    ("项目", "知识库", "已入库两本小说并建设定卡（RAG+别名融合）"),
    ("项目", "当前版本", "v4.8 主题系统（4套配色）"),
]


def refresh_progress_facts() -> int:
    """课程/项目进度事实刷新（主人专属）。

    upsert 语义（按 subject+predicate）：首轮迁移后 id 保持稳定，
    不会因"删了重插"把事实推到注入窗口外（曾因此丢进度）。
    """
    from app.core.memory import owner_user_id

    uid = owner_user_id()
    conn = connect()
    try:
        # 清理已不在快照中的课程/项目类事实（如课程被删除时）
        keep = {s for s, _, _ in PROGRESS_FACTS}
        ph = ",".join("?" * len(keep))
        conn.execute(
            "DELETE FROM facts WHERE user_id=? AND (subject LIKE '第%课' OR subject='六课带教计划' "
            "OR subject='项目') AND subject NOT IN (" + ph + ")",
            (uid,) + tuple(keep),
        )
        for sub, pred, obj in PROGRESS_FACTS:
            row = conn.execute(
                "SELECT id FROM facts WHERE user_id=? AND subject=? AND predicate=?",
                (uid, sub, pred),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE facts SET object=?, confidence=0.9 WHERE id=?",
                    (obj, row["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO facts (user_id, subject, predicate, object, confidence, updated_at) "
                    "VALUES (?, ?, ?, ?, 0.9, ?)",
                    (uid, sub, pred, obj, "2026-08-28T00:00:00+00:00"),
                )
        conn.commit()
    finally:
        conn.close()
    return len(PROGRESS_FACTS)


# 不灌进知识库的文档：内容以"举例"为主，会成为检索污染源。
# LESSONS.md 里写满了「左志诚被谁挖走了命丛」这类用来说明踩坑的剧情引用，
# 实测它反复出现在剧情问题的命中里——我们写的踩坑文档变成了检索噪声
# （自我污染）。这类文档是给人读的，不是给检索用的。
KNOWLEDGE_EXCLUDE = frozenset({"LESSONS", "TESTING_GUIDE", "AI_OPTIMIZATION_PROMPTS"})


async def sync_docs_to_knowledge() -> int:
    """docs/*.md 重灌知识库（ingest replace 语义，无重复）。"""
    docs = sorted(DOCS_DIR.glob("*.md"))
    total = 0
    for md in docs:
        if md.stem in KNOWLEDGE_EXCLUDE:
            logger.info("文档同步跳过（检索污染源）: %s", md.name)
            continue
        result = await knowledge.ingest_document(md.stem, md.read_text(encoding="utf-8"))
        total += result.get("chunks", 0)
        logger.info("文档同步: %s → %d 块", md.name, result.get("chunks", 0))
    return total


def purge_excluded_docs() -> int:
    """清掉已在库里的排除文档（连同向量与 FTS 索引）。返回删除块数。"""
    from app.models.database import connect

    conn = connect()
    try:
        removed = 0
        for name in KNOWLEDGE_EXCLUDE:
            rows = conn.execute(
                "SELECT id FROM knowledge_chunks WHERE doc_name=?", (name,)
            ).fetchall()
            for r in rows:
                for tbl, col in (("chunk_vectors", "chunk_id"),
                                 ("knowledge_fts", "chunk_id")):
                    try:
                        conn.execute(f"DELETE FROM {tbl} WHERE {col}=?", (r["id"],))
                    except Exception:  # noqa: BLE001 — 向量表可能不可用
                        pass
            cur = conn.execute("DELETE FROM knowledge_chunks WHERE doc_name=?", (name,))
            removed += cur.rowcount
        conn.commit()
        if removed:
            logger.info("清理检索污染源文档：%d 块", removed)
        return removed
    finally:
        conn.close()


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