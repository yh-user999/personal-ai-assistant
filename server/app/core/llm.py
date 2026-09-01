"""LLM 客户端：OpenAI 兼容协议（DeepSeek / 任意兼容端点）+ token 用量记账。

记账的动因：此前完全不记 usage，"这个功能烧多少 token"只能靠字符数估算，
而缓存读取比输入便宜 30 倍（zen 上 $0.007 vs $0.22 /M），估算误差可达数十倍。
现在每次调用把真实 usage 累加到进程内计数器，需要时可查/可清零。
只记内存不入库——记账本身不该产生写库开销，进程重启清零可接受
（要的是"这轮操作花了多少"，不是财务审计）。
"""
import logging
import threading

from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger("assistant.llm")

_client: AsyncOpenAI | None = None

# ── token 用量记账（进程内累计）──────────────────────────────
_usage_lock = threading.Lock()
_usage = {"calls": 0, "prompt": 0, "completion": 0, "cached": 0}


def _record_usage(resp) -> None:
    """累加一次调用的真实 usage。字段缺失一律跳过，绝不因记账影响主流程。"""
    u = getattr(resp, "usage", None)
    if u is None:
        return
    # 缓存命中数各家字段名不一：DeepSeek 原生用 prompt_cache_hit_tokens，
    # OpenAI 系用 prompt_tokens_details.cached_tokens，中转可能两者都不给
    cached = getattr(u, "prompt_cache_hit_tokens", 0) or 0
    if not cached:
        details = getattr(u, "prompt_tokens_details", None)
        if details is not None:
            cached = (
                getattr(details, "cached_tokens", 0)
                if not isinstance(details, dict)
                else details.get("cached_tokens", 0)
            ) or 0
    with _usage_lock:
        _usage["calls"] += 1
        _usage["prompt"] += getattr(u, "prompt_tokens", 0) or 0
        _usage["completion"] += getattr(u, "completion_tokens", 0) or 0
        _usage["cached"] += cached


def get_usage() -> dict:
    """当前进程累计用量快照。"""
    with _usage_lock:
        return dict(_usage)


def reset_usage() -> dict:
    """取回并清零（用于"测某段操作花了多少"）。"""
    with _usage_lock:
        snapshot = dict(_usage)
        for k in _usage:
            _usage[k] = 0
    return snapshot


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
    try:
        _record_usage(resp)
    except Exception:  # 记账失败绝不能影响回复
        logger.debug("usage 记账失败", exc_info=True)
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
