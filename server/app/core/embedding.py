"""Embedding 客户端：OpenAI 兼容协议（硅基流动 Qwen3-Embedding 等）。"""
from openai import AsyncOpenAI

from app.config import settings

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
        )
    return _client


async def embed(texts: list[str]) -> list[list[float]]:
    """批量向量化。返回与 texts 等长的向量列表。"""
    resp = await get_client().embeddings.create(
        model=settings.embedding_model,
        input=texts,
    )
    return [d.embedding for d in resp.data]
