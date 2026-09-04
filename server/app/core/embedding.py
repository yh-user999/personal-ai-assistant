"""Embedding 客户端：OpenAI 兼容协议（智谱 BigModel embedding-3）。"""
from openai import AsyncOpenAI

from app.config import settings

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            timeout=settings.llm_timeout,
            max_retries=settings.llm_max_retries,
        )
    return _client


async def embed(texts: list[str]) -> list[list[float]]:
    """批量向量化。返回与 texts 等长的向量列表。"""
    resp = await get_client().embeddings.create(
        model=settings.embedding_model,
        input=texts,
    )
    return [d.embedding for d in resp.data]


async def embed_batched(texts: list[str], batch_size: int = 8) -> list[list[float]]:
    """分批向量化：长文档全部块一次请求会超 API 上限（如 embedding-3 单次≤8 条）。

    逐批调用拼接，批间隔由 SDK 超时/重试兜底；适合小说等大文档入库。
    """
    import logging

    logger = logging.getLogger("assistant.embedding")
    out: list[list[float]] = []
    total = (len(texts) + batch_size - 1) // batch_size
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        resp = await get_client().embeddings.create(
            model=settings.embedding_model,
            input=batch,
        )
        out.extend(d.embedding for d in resp.data)
        n = i // batch_size + 1
        if n % 25 == 0 or n == total:
            logger.info("向量化进度 %d/%d 批", n, total)
    return out
