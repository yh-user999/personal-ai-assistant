"""LLM 客户端：OpenAI 兼容协议（DeepSeek / 任意兼容端点）。"""
from openai import AsyncOpenAI

from app.config import settings

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout,
            max_retries=settings.llm_max_retries,
        )
    return _client


async def chat(
    messages: list[dict],
    *,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    response_format: dict | None = None,
) -> str:
    """通用对话调用。messages: [{"role": ..., "content": ...}]"""
    kwargs = dict(model=settings.llm_model, messages=messages, temperature=temperature, max_tokens=max_tokens)
    if response_format:
        kwargs["response_format"] = response_format
    resp = await get_client().chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


async def chat_json(system: str, user: str) -> dict:
    """结构化 JSON 输出（用于摘要整合/画像/周报）。"""
    import json

    text = await chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 兜底：截取第一个 { 到最后一个 }
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        return {}
