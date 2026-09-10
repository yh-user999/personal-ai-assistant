"""GDELT 全球新闻事件库检索源（走代理，与国内直连隔离）。

## 定位

作为 SearXNG 之外的第二检索源，缓解"同一事件就那几篇转载"：GDELT 覆盖
全球媒体、天然多来源，配合来源独立性核查能真正体现"多方 vs 单一信源"。

## 代理隔离

GDELT 在境外，本机通过代理访问。本模块用**独立的** httpx client 并显式传
proxy，只作用于 GDELT；其它检索（SearXNG 等国内源）仍用 trust_env=False
的直连 client，物理隔离，不受影响。

## 速率限制

GDELT 公共 API 限"每 5 秒一次"，超了返回 429 纯文本。本模块内建进程级
最小间隔节流 + 429 静默降级：拿不到就返回空列表，绝不抛出、绝不阻断主链路
（SearXNG 有结果就正常回答）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger("assistant.gdelt")

_API = "https://api.gdeltproject.org/api/v2/doc/doc"

# time_range → GDELT timespan
_TIMESPAN = {"day": "1d", "week": "1w", "month": "1m", "year": "12m"}

# 进程级节流：GDELT 要求最小间隔（留余量用 6 秒）
_MIN_INTERVAL = 6.0
_last_call = 0.0
_lock = asyncio.Lock()


def _proxy() -> str:
    from app.config import settings

    return str(getattr(settings, "gdelt_proxy", "") or "").strip()


def configured() -> bool:
    from app.config import settings

    return bool(getattr(settings, "gdelt_enabled", False))


def _seendate_to_iso(raw: str) -> str:
    """GDELT seendate 形如 20260907T224500Z → 2026-09-07T22:45:00Z。"""
    s = (raw or "").strip()
    if len(s) < 15 or "T" not in s:
        return ""
    d, _, t = s.partition("T")
    if len(d) != 8 or len(t) < 6:
        return ""
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}T{t[:2]}:{t[2:4]}:{t[4:6]}Z"


def _map_article(raw: dict[str, Any]) -> dict[str, Any] | None:
    """GDELT 文章 → 与 web_provider.normalize_result 一致的 schema。"""
    if not isinstance(raw, dict):
        return None
    url = str(raw.get("url") or "").strip()
    title = str(raw.get("title") or "").strip()
    domain = str(raw.get("domain") or "").strip()
    if not url or not title or not domain:
        return None
    published = _seendate_to_iso(str(raw.get("seendate") or ""))
    return {
        "title": title[:300],
        "url": url[:1000],
        "source": domain[:120],
        "published_at": published[:40],
        "time_known": bool(published),
        # GDELT artlist 不带正文摘要，留空——下游据来源+标题处理
        "summary": "",
        "language": str(raw.get("language") or "").strip()[:20],
    }


async def search(
    query: str,
    *,
    time_range: str = "week",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """检索 GDELT；未启用/被限流/失败一律返回空列表，绝不抛出。"""
    if not configured():
        return []
    text = (query or "").strip()
    if not text:
        return []

    from app.config import settings

    timeout = float(getattr(settings, "gdelt_timeout", 15.0))
    cap = max(1, min(int(limit), int(getattr(settings, "gdelt_max_results", 10))))
    timespan = _TIMESPAN.get(time_range, "1m") if time_range else "1m"
    params = {
        "query": text[:200],
        "mode": "artlist",
        "format": "json",
        "maxrecords": str(cap),
        "timespan": timespan,
        "sort": "datedesc",
    }

    # 进程级节流：抢锁后确保距上次调用 ≥ 最小间隔
    global _last_call
    async with _lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            # 节流等待有上限：超过就放弃本轮（不拖慢主链路）
            if wait > max(1.0, timeout - 2.0):
                logger.info("GDELT 节流等待过长（%.1fs），本轮跳过", wait)
                return []
            await asyncio.sleep(wait)
        _last_call = time.monotonic()

    proxy = _proxy() or None
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False, proxy=proxy) as client:
            resp = await client.get(_API, params=params)
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.warning("GDELT 请求失败（%s），本轮跳过", type(exc).__name__)
        return []

    # 429 或非 JSON（GDELT 限流返回纯文本提示）→ 静默降级
    if resp.status_code != 200 or not resp.text.lstrip().startswith("{"):
        if resp.status_code == 429:
            logger.info("GDELT 被限流（429），本轮跳过")
        else:
            logger.warning("GDELT 返回异常（HTTP %s），本轮跳过", resp.status_code)
        return []

    try:
        payload = resp.json()
    except (ValueError, TypeError):
        return []
    arts = payload.get("articles") if isinstance(payload, dict) else None
    if not isinstance(arts, list):
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in arts:
        item = _map_article(raw)
        if item is None or item["url"] in seen:
            continue
        seen.add(item["url"])
        out.append(item)
        if len(out) >= cap:
            break
    return out
