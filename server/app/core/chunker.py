"""文档切块器：把长文档切成适合检索的文本块。

策略（教学级实现，与 12 周项目对照）：
1. 按空行分段（自然段落边界优先，避免切断语义）
2. 段落累积到 chunk_size 上限即封块
3. 超长段落按固定长度硬切，重叠 overlap 字防"断义"
"""
import re

DEFAULT_CHUNK_SIZE = 500   # 每块约 500 字
DEFAULT_OVERLAP = 50       # 重叠 50 字


def chunk_text(text: str, chunk_size: int = DEFAULT_CHUNK_SIZE,
               overlap: int = DEFAULT_OVERLAP) -> list[str]:
    """返回文本块列表。"""
    # 统一换行 + 按空行分自然段
    text = text.replace("\r\n", "\n")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        # 段落本身超长 → 先封当前缓冲，再硬切
        if len(para) > chunk_size:
            if buf.strip():
                chunks.append(buf.strip())
                buf = ""
            while len(para) > chunk_size:
                chunks.append(para[:chunk_size].strip())
                para = para[chunk_size - overlap:]
        if len(buf) + len(para) + 1 <= chunk_size:
            buf += para + "\n"
        else:
            chunks.append(buf.strip())
            buf = para + "\n"
    if buf.strip():
        chunks.append(buf.strip())
    return [c for c in chunks if c]
