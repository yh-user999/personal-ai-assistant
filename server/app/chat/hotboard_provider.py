"""国内热榜检索源：今日头条 + 百度热搜（直连，不走代理）。

## 定位

回答"最近有什么大事/现在大家在讨论什么"这类**浏览型**问题——SearXNG 的
关键词检索覆盖不到"当下热点榜单"。两者互补：热榜答"最近热点"，
SearXNG 答"查具体事件"。

## 边界（重要）

热榜条目是**当下热议话题**，不是已核实事实。注入回复时必须标注"话题清单，
具体事实需进一步核实"，不能把热榜标题当成已证实的事件。

## 隔离

国内源直连（trust_env=False，不读环境代理），与走代理的 GDELT 互不影响。
任一源失败只跳过该源；全失败返回空，绝不抛出、绝不拖垮主链路。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger("assistant.hotboard")

_TOUTIAO = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"
_BAIDU = "https://top.baidu.com/api/board?platform=wise&tab=realtime"
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def configured() -> bool:
    from app.config import settings

    return bool(getattr(settings, "hotboard_enabled", False))


async def _fetch_toutiao(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """今日头条热榜。失败返回空。"""
    try:
        r = await client.get(_TOUTIAO, headers=_UA)
        if r.status_code != 200 or not r.text.lstrip().startswith("{"):
            return []
        data = r.json().get("data")
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.info("头条热榜失败（%s）", type(exc).__name__)
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for it in data:
        if not isinstance(it, dict):
            continue
        title = str(it.get("Title") or "").strip()
        if not title:
            continue
        out.append({
            "title": title[:200],
            "url": str(it.get("Url") or "").strip()[:1000],
            "source": "今日头条热榜",
            "hot": str(it.get("HotValue") or "").strip()[:20],
        })
    return out


async def _fetch_baidu(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """百度实时热搜。失败返回空。"""
    try:
        r = await client.get(_BAIDU, headers=_UA)
        if r.status_code != 200 or not r.text.lstrip().startswith("{"):
            return []
        cards = r.json().get("data", {}).get("cards") or []
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.info("百度热搜失败（%s）", type(exc).__name__)
        return []
    if not cards or not isinstance(cards, list):
        return []
    content = cards[0].get("content") if isinstance(cards[0], dict) else None
    if not isinstance(content, list):
        return []
    out: list[dict[str, Any]] = []
    for it in content:
        if not isinstance(it, dict):
            continue
        title = str(it.get("word") or it.get("query") or "").strip()
        if not title:
            continue
        out.append({
            "title": title[:200],
            "url": str(it.get("url") or "").strip()[:1000],
            "source": "百度热搜",
            "hot": str(it.get("hotScore") or "").strip()[:20],
        })
    return out


def _norm(title: str) -> str:
    import re

    return re.sub(r"[\s\u3000，。、；：!！?？…\-—\"'“”]", "", title)


def _merge_dedup(lists: list[list[dict[str, Any]]], cap: int) -> list[dict[str, Any]]:
    """合并多源热榜，按归一化标题去重（先到先留，保留原榜序）。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    # 交错合并，避免某一源霸榜：轮流从各源取
    idx = 0
    while len(out) < cap:
        progressed = False
        for lst in lists:
            if idx < len(lst):
                progressed = True
                item = lst[idx]
                key = _norm(item["title"])
                if key and key not in seen:
                    seen.add(key)
                    out.append(item)
                    if len(out) >= cap:
                        break
        idx += 1
        if not progressed:
            break
    return out


async def fetch_hotboard(limit: int | None = None) -> list[dict[str, Any]]:
    """并发拉取多源热榜并合并去重；未启用或全失败返回空。"""
    if not configured():
        return []

    from app.config import settings

    timeout = float(getattr(settings, "hotboard_timeout", 6.0))
    cap = int(limit or getattr(settings, "hotboard_max_items", 15))
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=True) as client:
            toutiao, baidu = await asyncio.gather(
                _fetch_toutiao(client), _fetch_baidu(client),
                return_exceptions=True,
            )
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.warning("热榜拉取失败（%s）", type(exc).__name__)
        return []
    lists = [x for x in (toutiao, baidu) if isinstance(x, list)]
    return _merge_dedup(lists, cap)


def format_hotboard(items: list[dict[str, Any]], limit: int = 15) -> str:
    """格式化为注入 prompt 的热点清单。"""
    if not items:
        return ""
    lines = ["【当前热点话题（来自今日头条/百度热搜，是热议话题清单，非已核实事实）】"]
    for i, it in enumerate(items[:limit], 1):
        src = it.get("source", "")
        lines.append(f"{i}. {it['title']}（{src}）")
    return "\n".join(lines)
