"""文档生成与保存：对话式"写文档"→ LLM 生成 → 存 documents 表 + 同步知识库。

保存后的文档自动 ingest 进知识库（切块+向量化），之后可检索/问答。
"""
import re
from datetime import datetime, timezone

from app.core import knowledge, llm
from app.models.database import connect

DOC_PROMPT = """你是文档撰写助手。根据用户给出的标题与要求，生成一份结构化的 Markdown 文档。
要求：
- 标题用 # 开头
- 内容分小节（## 标题），条理清晰
- 基于用户要求写实，不编造用户没说过的信息
只输出文档正文（Markdown），不要多余解释。
"""


def parse_doc_command(msg: str) -> tuple[str, str] | None:
    """解析"写文档"命令 → (标题, 要求)。非命令返回 None。

    支持："写文档：标题XXX，内容：YYY" / "写文档 标题XXX" 等口语变体。
    """
    m = re.match(r"^(?:写文档|生成文档|写一份文档|帮我写文档)[:：]?\s*(.+)$", msg.strip())
    if not m:
        return None
    rest = m.group(1).strip()
    # "标题：X，内容：Y" / "标题X，内容：Y" / "X，内容：Y"
    title, requirement = rest, ""
    tm = re.match(r"标题[:：]?\s*(.+?)\s*[，,]\s*内容[:：]\s*(.+)$", rest)
    if tm:
        title, requirement = tm.group(1).strip(), tm.group(2).strip()
    else:
        cm = re.match(r"(.+?)\s*[，,]\s*内容[:：]\s*(.+)$", rest)
        if cm:
            title, requirement = cm.group(1).strip(), cm.group(2).strip()
    # 兜底：标题可能残留"标题"前缀（如"写文档：标题周报"无逗号时）
    title = re.sub(r"^标题[:：]?\s*", "", title).strip()
    return (title[:60], requirement or title)


async def generate_and_save(title: str, requirement: str) -> dict:
    """LLM 生成文档 → 存 documents 表 → 同步知识库。返回 {id, title, words}。"""
    content = await llm.chat(
        [
            {"role": "system", "content": DOC_PROMPT},
            {"role": "user", "content": f"标题：{title}\n要求：{requirement}"},
        ],
        temperature=0.4,
        max_tokens=3000,
    )
    content = content.strip()
    if not content:
        return {"error": "生成失败：LLM 未返回内容"}

    now = datetime.now(timezone.utc).isoformat()
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO documents (title, content, created_at) VALUES (?, ?, ?)",
            (title, content, now),
        )
        doc_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    # 同步进知识库（切块+向量化，之后可检索）
    try:
        await knowledge.ingest_document(f"文档-{title}", content)
    except Exception:
        pass  # 知识库同步失败不阻塞文档保存

    return {"id": doc_id, "title": title, "words": len(content)}


def list_documents(limit: int = 20) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, title, created_at FROM documents ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_document(doc_id: int) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, title, content, created_at FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None
