"""Embedding 客户端：OpenAI 兼容协议（智谱 BigModel embedding-3）。"""
from contextlib import contextmanager
from contextvars import ContextVar

from openai import AsyncOpenAI

from app.config import settings

_client: AsyncOpenAI | None = None

# 请求级同文本去重缓存：retrieve() 外层开一次作用域，作用域内同一文本的
# 多次 embed 只打一次 API（聊天检索里 memory.search 与 knowledge._vector_search
# 会对同一 search_query 各调一次，单次 218~1244ms）。作用域只由检索链路打开，
# 灌库批处理等长任务不进缓存，无内存膨胀。to_thread 工作线程复制上下文时
# 共享同一 dict 对象，跨线程去重同样生效。
_query_scope: ContextVar[dict[str, list[list[float]]] | None] = ContextVar(
    "embedding_query_scope", default=None
)


@contextmanager
def query_scope():
    """打开请求级 embed 去重作用域（用法：``with embedding.query_scope():``）。"""
    token = _query_scope.set({})
    try:
        yield
    finally:
        _query_scope.reset(token)


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            timeout=settings.llm_timeout,
            # SDK 内建重试必须关闭（与 core/llm.py 同一策略）：大批量灌库时
            # SDK 逐层重试会与调用方的分批节奏叠加成请求风暴。
            max_retries=0,
        )
    return _client


async def embed(texts: list[str]) -> list[list[float]]:
    """批量向量化。返回与 texts 等长的向量列表。

    处于 query_scope() 作用域内时按文本去重：已算过的文本直接复用缓存向量。
    """
    cache = _query_scope.get()
    if cache is None:
        return await _embed_api(texts)
    results: list[list[float] | None] = [cache.get(t) for t in texts]
    missing = [t for t, r in zip(texts, results) if r is None]
    if missing:
        fresh = await _embed_api(missing)
        for t, v in zip(missing, fresh):
            cache[t] = v
    return [cache[t] for t in texts]


async def _embed_api(texts: list[str]) -> list[list[float]]:
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
