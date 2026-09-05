"""LLM 客户端池：OpenAI 兼容协议、多 Key 故障切换与 token 用量记账。

Key 只从配置读取，日志和可观测性只记录序号/脱敏指纹，不输出密钥原文。
每次请求最多按候选 Key 顺序尝试一轮；仅临时性错误触发切换，参数/权限类
错误直接保留原始异常，避免重复扣费式重试。
"""
from __future__ import annotations

import asyncio
import copy
import logging
import threading
import time
import uuid
import weakref
from dataclasses import dataclass

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAIError,
    RateLimitError,
)

from app.config import mask_api_key, settings

logger = logging.getLogger("assistant.llm")

# 兼容旧调用方：get_client() 仍返回第一个 Key 的客户端。
_client: AsyncOpenAI | None = None
_clients: dict[int, AsyncOpenAI] = {}
_pool_signature: tuple | None = None
_pool_lock = threading.RLock()
_key_states: dict[int, _KeyState] = {}
_next_key_index = 0

_RETRYABLE_STATUS_CODES = frozenset({402, 429})
_semaphore_lock = threading.Lock()
_loop_semaphores: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


@dataclass
class _KeyState:
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    last_error: str = ""


# ── token 用量记账（进程内累计）──────────────────────────────
_usage_lock = threading.Lock()
_usage = {"calls": 0, "prompt": 0, "completion": 0, "cached": 0}


def _new_observability() -> dict:
    return {
        "requests": 0,
        "fallback_count": 0,
        "by_key": {},
        "by_model": {},
        "failures": {},
        "last_call": None,
    }


_observability = _new_observability()

# ── 小说模型启动检查状态 ────────────────────────────────────
_model_lock = threading.Lock()
_model_check_signature: tuple | None = None
_effective_novel_model: str | None = None


def _api_keys() -> list[str]:
    """取规范化 Key 列表；配置错误交给 config 的可诊断校验抛出。"""
    return settings.llm_api_key_values


def _concurrency_semaphore() -> asyncio.Semaphore:
    """为当前事件循环提供全局 LLM 并发闸门。"""
    try:
        limit = max(1, int(settings.llm_max_concurrency))
    except (TypeError, ValueError):
        limit = 8
    loop = asyncio.get_running_loop()
    with _semaphore_lock:
        semaphore = _loop_semaphores.get(loop)
        if semaphore is None:
            semaphore = asyncio.Semaphore(limit)
            _loop_semaphores[loop] = semaphore
        return semaphore


def _pool_config_signature(keys: list[str]) -> tuple:
    return (
        settings.llm_base_url,
        settings.llm_timeout,
        settings.llm_max_retries,
        tuple(keys),
    )


def _ensure_pool_locked(keys: list[str]) -> None:
    """配置变更后丢弃旧池状态，避免测试/热更新继续使用旧 Key。"""
    global _pool_signature, _clients, _key_states, _next_key_index, _client
    signature = _pool_config_signature(keys)
    if signature == _pool_signature:
        return
    _clients = {}
    _key_states = {index: _KeyState() for index in range(len(keys))}
    _next_key_index = 0
    _pool_signature = signature
    _client = None


def reset_client_pool() -> None:
    """清空客户端池和 Key 冷却状态，主要供测试/配置热切换使用。"""
    global _pool_signature, _clients, _key_states, _next_key_index, _client
    global _model_check_signature, _effective_novel_model
    with _pool_lock:
        _clients = {}
        _key_states = {}
        _pool_signature = None
        _next_key_index = 0
        _client = None
    with _model_lock:
        _model_check_signature = None
        _effective_novel_model = None


def get_client(key_index: int = 0) -> AsyncOpenAI:
    """按 Key 序号懒加载客户端；不接受/返回任何密钥诊断信息。"""
    global _client
    keys = _api_keys()
    if not keys:
        raise RuntimeError("未配置 LLM_API_KEY 或 LLM_API_KEYS，无法调用 LLM")
    if key_index < 0 or key_index >= len(keys):
        raise IndexError(f"LLM Key 序号越界：{key_index}")

    with _pool_lock:
        _ensure_pool_locked(keys)
        client = _clients.get(key_index)
        if client is None:
            client = AsyncOpenAI(
                base_url=settings.llm_base_url,
                api_key=keys[key_index],
                timeout=settings.llm_timeout,
                # SDK 自带重试必须关闭；重试预算由 chat() 统一管理，避免
                # 应用层切 Key 与 SDK 内层重试叠加造成请求风暴。
                max_retries=0,
            )
            _clients[key_index] = client
        if key_index == 0:
            _client = client
        return client


def _candidate_key_indices(keys: list[str]) -> list[int]:
    """返回本轮 Key 顺序；优先跳过冷却中的 Key，全部冷却时仍探测一轮。"""
    global _next_key_index
    with _pool_lock:
        _ensure_pool_locked(keys)
        total = len(keys)
        start = _next_key_index % total
        # 先推进默认起点，成功后会再精确调整到成功 Key 的下一个。
        _next_key_index = (start + 1) % total
        order = [(start + offset) % total for offset in range(total)]
        now = time.monotonic()
        available = [
            index
            for index in order
            if _key_states[index].cooldown_until <= now
        ]
        if available:
            return available
        logger.warning("所有 LLM Key 均在冷却期，按轮询顺序进行恢复探测")
        return order


def _status_code(exc: BaseException) -> int | None:
    value = getattr(exc, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def is_retryable_error(exc: BaseException) -> bool:
    """仅识别额度/限流/临时网关/网络类错误。"""
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, APIStatusError):
        status = _status_code(exc)
        return status in _RETRYABLE_STATUS_CODES or (status is not None and 500 <= status <= 599)
    status = _status_code(exc)
    if status in _RETRYABLE_STATUS_CODES or (status is not None and 500 <= status <= 599):
        return True
    return isinstance(
        exc,
        (
            APIConnectionError,
            APITimeoutError,
            httpx.TimeoutException,
            httpx.NetworkError,
            TimeoutError,
            ConnectionError,
        ),
    )


# 测试和旧内部调用方可能使用私有命名。
_is_retryable_error = is_retryable_error


def _failure_reason(exc: BaseException) -> str:
    status = _status_code(exc)
    if status is not None:
        return f"http_{status}"
    if isinstance(exc, (APITimeoutError, httpx.TimeoutException, TimeoutError)):
        return "timeout"
    if isinstance(exc, (APIConnectionError, httpx.NetworkError, ConnectionError)):
        return "connection"
    return type(exc).__name__


def _key_bucket(index: int) -> dict:
    return _observability["by_key"].setdefault(
        str(index),
        {
            "requests": 0,
            "fallbacks": 0,
            "failures": 0,
            "prompt": 0,
            "completion": 0,
            "cached": 0,
            "last_error": "",
        },
    )


def _model_bucket(model: str) -> dict:
    return _observability["by_model"].setdefault(
        model,
        {"requests": 0, "prompt": 0, "completion": 0, "cached": 0},
    )


def _record_request(*, key_index: int, model: str, fallback_count: int) -> None:
    """记录一次成功请求的路由元数据，不记录请求内容或密钥。"""
    with _usage_lock:
        bucket = _key_bucket(key_index)
        model_bucket = _model_bucket(model)
        _observability["requests"] += 1
        _observability["fallback_count"] += fallback_count
        bucket["requests"] += 1
        bucket["fallbacks"] += fallback_count
        model_bucket["requests"] += 1
        _observability["last_call"] = {
            "key_index": key_index,
            "model": model,
            "fallback_count": fallback_count,
        }


def _record_failure(key_index: int, reason: str) -> None:
    with _usage_lock:
        bucket = _key_bucket(key_index)
        bucket["failures"] += 1
        bucket["last_error"] = reason
        by_reason = _observability["failures"].setdefault(str(key_index), {})
        by_reason[reason] = by_reason.get(reason, 0) + 1


def _record_usage(
    resp,
    *,
    key_index: int | None = None,
    model: str | None = None,
    request_id: str = "",
    user_id: str | None = None,
    fallback_count: int = 0,
) -> None:
    """累加一次调用的真实 usage，并在有 request_id 时写入持久化表。"""
    u = getattr(resp, "usage", None)
    if u is None:
        return
    # 缓存命中数各家字段名不一：DeepSeek 原生用 prompt_cache_hit_tokens，
    # OpenAI 系用 prompt_tokens_details.cached_tokens，中转可能两者都不给。
    cached = getattr(u, "prompt_cache_hit_tokens", 0) or 0
    if not cached:
        details = getattr(u, "prompt_tokens_details", None)
        if details is not None:
            cached = (
                getattr(details, "cached_tokens", 0)
                if not isinstance(details, dict)
                else details.get("cached_tokens", 0)
            ) or 0
    prompt = int(getattr(u, "prompt_tokens", 0) or 0)
    completion = int(getattr(u, "completion_tokens", 0) or 0)
    cached = int(cached)
    with _usage_lock:
        _usage["calls"] += 1
        _usage["prompt"] += prompt
        _usage["completion"] += completion
        _usage["cached"] += cached
        if key_index is not None:
            bucket = _key_bucket(key_index)
            bucket["prompt"] += prompt
            bucket["completion"] += completion
            bucket["cached"] += cached
        if model:
            model_bucket = _model_bucket(model)
            model_bucket["prompt"] += prompt
            model_bucket["completion"] += completion
            model_bucket["cached"] += cached

    if request_id:
        try:
            from app.services.llm_usage import record

            record(
                request_id=request_id,
                user_id=user_id,
                model=model or "",
                key_index=key_index or 0,
                prompt_tokens=prompt,
                completion_tokens=completion,
                cached_tokens=cached,
                fallback_count=fallback_count,
            )
        except Exception as exc:  # noqa: BLE001
            # 用量落库失败不能影响已经拿到的模型响应；日志不暴露 prompt/key。
            logger.warning("LLM 用量持久化失败: %s", type(exc).__name__)


def get_usage() -> dict:
    """当前进程累计用量快照（保留旧的四字段返回契约）。"""
    with _usage_lock:
        return dict(_usage)


def _key_status_snapshot() -> list[dict]:
    keys = _api_keys()
    if not keys:
        return []
    now = time.monotonic()
    with _pool_lock:
        _ensure_pool_locked(keys)
        return [
            {
                "key_index": index,
                "fingerprint": mask_api_key(key, index=index),
                "consecutive_failures": _key_states[index].consecutive_failures,
                "cooldown_remaining": round(
                    max(0.0, _key_states[index].cooldown_until - now), 1
                ),
                "last_error": _key_states[index].last_error,
            }
            for index, key in enumerate(keys)
        ]


def get_usage_details() -> dict:
    """返回用量、切换、失败原因及 Key 状态的可观测性快照。"""
    with _usage_lock:
        details = copy.deepcopy(_observability)
    details["usage"] = get_usage()
    details["key_status"] = _key_status_snapshot()
    return details


def reset_usage() -> dict:
    """取回并清零旧用量及新增可观测性计数。"""
    global _observability
    with _usage_lock:
        snapshot = dict(_usage)
        for key in _usage:
            _usage[key] = 0
        _observability = _new_observability()
    return snapshot


def _mark_success(key_index: int) -> None:
    global _next_key_index
    keys = _api_keys()
    with _pool_lock:
        _ensure_pool_locked(keys)
        state = _key_states[key_index]
        state.consecutive_failures = 0
        state.cooldown_until = 0.0
        state.last_error = ""
        _next_key_index = (key_index + 1) % len(keys)


def _mark_failure(key_index: int, exc: BaseException, *, cooldown: bool) -> str:
    reason = _failure_reason(exc)
    _record_failure(key_index, reason)
    keys = _api_keys()
    with _pool_lock:
        _ensure_pool_locked(keys)
        state = _key_states[key_index]
        if cooldown:
            state.consecutive_failures += 1
            base = max(0.0, float(settings.llm_key_cooldown_seconds))
            multiplier = min(2 ** (state.consecutive_failures - 1), 8)
            state.cooldown_until = time.monotonic() + base * multiplier
        state.last_error = reason
        fingerprint = mask_api_key(keys[key_index], index=key_index)
        remaining = max(0.0, state.cooldown_until - time.monotonic())
    if cooldown:
        logger.warning(
            "LLM %s 请求失败（%s），冷却 %.1f 秒",
            fingerprint,
            reason,
            remaining,
        )
    else:
        logger.warning("LLM %s 返回不可切换错误（%s），不切换 Key", fingerprint, reason)
    return reason


def _model_signature() -> tuple:
    return (
        settings.llm_base_url,
        settings.llm_model,
        settings.novel_llm_model,
        tuple(_api_keys()),
    )


def get_novel_model() -> str:
    """返回小说链路当前生效模型；未完成启动检查时使用配置值。"""
    desired = str(settings.novel_llm_model or "").strip()
    fallback = str(settings.llm_model or "").strip()
    signature = _model_signature()
    with _model_lock:
        if _model_check_signature == signature and _effective_novel_model:
            return _effective_novel_model
    return desired or fallback


def get_vision_model() -> str:
    """返回图片识别专用模型；未配置时回退普通聊天模型。"""
    desired = str(getattr(settings, "vision_llm_model", "") or "").strip()
    return desired or str(settings.llm_model or "").strip()


def _set_effective_novel_model(model: str) -> None:
    global _model_check_signature, _effective_novel_model
    with _model_lock:
        _model_check_signature = _model_signature()
        _effective_novel_model = model


def _model_ids(response) -> set[str]:
    ids: set[str] = set()
    for item in getattr(response, "data", None) or []:
        value = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
        if value:
            ids.add(str(value))
    return ids


async def validate_novel_model() -> str:
    """启动期检查小说模型；不可达或不存在时只告警并回退全局模型。"""
    desired = str(settings.novel_llm_model or "").strip()
    fallback = str(settings.llm_model or "").strip()
    if not desired or desired == fallback:
        _set_effective_novel_model(fallback)
        return fallback

    try:
        keys = _api_keys()
    except ValueError as exc:
        logger.warning("小说模型检查跳过（Key 配置无效：%s），使用全局模型", type(exc).__name__)
        _set_effective_novel_model(fallback)
        return fallback
    if not keys:
        logger.warning("小说模型检查跳过（未配置 LLM Key），使用全局模型")
        _set_effective_novel_model(fallback)
        return fallback

    order = _candidate_key_indices(keys)
    last_reason = "unknown"
    for position, key_index in enumerate(order):
        try:
            response = await get_client(key_index).models.list(
                timeout=min(max(float(settings.llm_timeout), 1.0), 5.0)
            )
        except (OpenAIError, httpx.HTTPError, TimeoutError, ConnectionError) as exc:
            retryable = is_retryable_error(exc)
            last_reason = _mark_failure(key_index, exc, cooldown=retryable)
            if retryable and position + 1 < len(order):
                continue
            logger.warning(
                "小说模型检查失败（%s），使用全局模型 %s",
                last_reason,
                fallback or "(未配置)",
            )
            _set_effective_novel_model(fallback)
            return fallback

        _mark_success(key_index)
        if desired in _model_ids(response):
            _set_effective_novel_model(desired)
            logger.info("小说模型可用：%s", desired)
            return desired
        logger.warning("小说模型 %s 不在服务商模型列表中，使用全局模型 %s", desired, fallback)
        _set_effective_novel_model(fallback)
        return fallback

    logger.warning("小说模型检查未找到可用 Key（%s），使用全局模型", last_reason)
    _set_effective_novel_model(fallback)
    return fallback


async def chat(
    messages: list[dict],
    *,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    response_format: dict | None = None,
    timeout: float | None = None,
    model: str | None = None,
    request_id: str | None = None,
    user_id: str | None = None,
) -> str:
    """通用对话调用，按 Key 池执行一次有限轮故障切换。

    ``model`` 为空时使用全局 ``LLM_MODEL``；小说链路应显式传入
    ``get_novel_model()``。同一请求最多尝试所有候选 Key 一轮。
    """
    selected_model = (model or settings.llm_model or "").strip()
    kwargs = {
        "model": selected_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format:
        kwargs["response_format"] = response_format
    if timeout is not None:
        kwargs["timeout"] = timeout

    keys = _api_keys()
    if not keys:
        raise RuntimeError("未配置 LLM_API_KEY 或 LLM_API_KEYS，无法调用 LLM")
    try:
        retry_budget = max(0, int(settings.llm_max_retries))
    except (TypeError, ValueError):
        retry_budget = 0
    max_attempts = retry_budget + 1
    request_id = str(request_id or uuid.uuid4().hex)[:160]

    order = _candidate_key_indices(keys)
    last_exc: BaseException | None = None
    async with _concurrency_semaphore():
        for attempt in range(max_attempts):
            key_index = order[attempt % len(order)]
            try:
                resp = await get_client(key_index).chat.completions.create(**kwargs)
            except Exception as exc:
                last_exc = exc
                retryable = is_retryable_error(exc)
                _mark_failure(key_index, exc, cooldown=retryable)
                if not retryable:
                    raise
                if attempt + 1 >= max_attempts:
                    break
                next_index = order[(attempt + 1) % len(order)]
                logger.info(
                    "LLM Key 故障切换：%s -> key[%d]，model=%s，原因=%s（第 %d/%d 次）",
                    mask_api_key(keys[key_index], index=key_index),
                    next_index,
                    selected_model,
                    _failure_reason(exc),
                    attempt + 1,
                    max_attempts,
                )
                try:
                    backoff = max(0.0, float(settings.llm_retry_backoff_seconds))
                except (TypeError, ValueError):
                    backoff = 0.0
                if backoff:
                    await asyncio.sleep(backoff * min(2 ** attempt, 8))
                continue

            _mark_success(key_index)
            fallback_count = attempt
            try:
                _record_request(
                    key_index=key_index,
                    model=selected_model,
                    fallback_count=fallback_count,
                )
                _record_usage(
                    resp,
                    key_index=key_index,
                    model=selected_model,
                    request_id=request_id,
                    user_id=user_id,
                    fallback_count=fallback_count,
                )
            except Exception:
                logger.debug("LLM 用量/路由记账失败", exc_info=True)
            return resp.choices[0].message.content or ""

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("LLM 请求未执行")


async def chat_json(
    system: str,
    user: str,
    *,
    model: str | None = None,
    request_id: str | None = None,
    user_id: str | None = None,
) -> dict:
    """结构化 JSON 输出（用于摘要整合/画像/周报）。"""
    import json

    text = await chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.2,
        response_format={"type": "json_object"},
        model=model,
        request_id=request_id,
        user_id=user_id,
    )
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 兜底：截取第一个 { 到最后一个 }
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        return {}
