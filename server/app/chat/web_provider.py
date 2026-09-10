"""实时信息检索 provider：网页/新闻搜索、抓页与事件聚合。

事实层来源。检索结果必须带 ``source`` 与 ``published_at``——没有来源的条目
一律不进回复，因为"无来源不判断"是价值层的安全底线。

设计要点：
- 后端为 SearXNG 自托管（JSON API），未配置时整体降级为"未查到"，绝不
  退回模型记忆作答；
- 抓页做 SSRF 护栏：只允许 http(s)、拒绝内网与环回地址，避免搜索结果
  把请求引向本机服务；
- 不引入 HTML 解析依赖，用正则剥离 script/style 与标签；
- 事件聚合按标题相似度 + 时间邻近归并，同一事件的多篇报道合成一条。
"""
from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("assistant.web")

# SearXNG time_range 取值
TIME_RANGES = frozenset({"day", "week", "month", "year"})

# 时效词：命中即要求联网，且禁止无来源作答
_FRESHNESS_RE = re.compile(
    r"最近|最新|近期|这几天|这两天|今天|昨天|前天|本周|这周|上午|下午|"
    r"晚上|今晚|中午|眼下|目前|近来|日前|如今|到哪了|怎么样了|如何了|进展如何|"
    r"刚发生|刚刚|实时|现在.*(?:新闻|情况|进展|怎么样|如何|什么|多少)"
)
# 事件名词：足以单独判定为事件查询（"…事件/案件/事故/通报"）
_EVENT_NOUN_RE = re.compile(r"事件|案件|事故|通报")
# 新闻类名词：与时效词组合才算联网查询，避免"最近的进展"这类自指误判
_NEWS_NOUN_RE = re.compile(r"新闻|消息|报道|时事|热搜|事件|案件|事故|通报")
# 求信息标记：在问"是什么/怎么样/多少"，而不是在陈述或下指令。
_INFO_SEEKING_RE = re.compile(
    r"[?？]|什么|哪些|哪个|怎么样|怎样|如何|多少|为什么|为啥|是否|吗|到哪|"
    r"情况|进展|动态|近况|现状|消息"
)
# 常见新闻主体：国家/地区与高频公共议题。是**安全网**不是穷举——主判在
# planner（其提示词已写明 web_search 的适用条件），这里只收高置信度信号。
_COMMON_ENTITIES = (
    "中国|美国|日本|俄罗斯|乌克兰|韩国|朝鲜|印度|英国|法国|德国|意大利|西班牙|"
    "加拿大|澳大利亚|巴西|伊朗|以色列|巴勒斯坦|土耳其|沙特|欧盟|北约|联合国|"
    "泰国|越南|菲律宾|新加坡|马来西亚|印尼|阿富汗|叙利亚|伊拉克|巴基斯坦|"
    "墨西哥|阿根廷|台湾|香港|澳门|中美|俄乌|中日|中欧"
)
_PUBLIC_TOPICS = (
    "台风|地震|洪水|暴雨|疫情|火灾|爆炸|空难|塌方|矿难|"
    "股市|A股|房价|物价|油价|金价|黄金|汇率|利率|关税|通胀|"
    "政策|法案|选举|峰会|谈判|制裁|停火|和谈"
)
# 外部主体：命中说明在谈外部世界，而不是用户或助手自身的事务。
# 误判的代价很实在——闲聊被迫联网，还会因"无来源只能说未查到"而答非所问，
# 所以每条都是结构信号，不含裸英文串（"最近 API 有什么变化"这类项目内
# 问法不该被拖去搜网）。
#
# 这里**不做音译人名识别**。试过按"音译用字连用"来认外来名，结果它既误判
# 又漏判：同样是"汉字+音译字"，"斯拉夫语""蒙特卡洛""伯克利"被算成新闻
# 主体，而"泽连斯基""普京"照样漏掉——字符级正则区分不了专名和普通词。
# 人名判定交给 planner（其提示词已写明 web_search 的适用条件），规则层
# 只保留能靠结构确认的信号：宁可漏，不可误判。
_EXTERNAL_SUBJECT_RE = re.compile(
    rf"(?:{_COMMON_ENTITIES})|(?:{_PUBLIC_TOPICS})|"
    r"[\u4e00-\u9fa5]{2,4}(?:省|市|县|区|镇|村|国|州|岛)|"
    r"[\u4e00-\u9fa5]{2,6}(?:大学|医院|公司|集团|银行|政府|法院|警方|军队|组织|协会|部门|研究院|委员会|管理局)|"
    r"(?:男童|女童|幼童|男孩|女孩|男子|女子|老人|学生|明星|网红|博主|官员|总统|总理|主席|部长|市长|省长|记者|专家)"
)
# 指代式追问："现在呢""后来呢"——本身没有主体，靠上一轮语境。
# 用 fullmatch 语义（^...$）以免吃掉"继续写第三章"这类创作指令。
_FOLLOWUP_RE = re.compile(
    r"^(?:那|再|还|又|接着|然后)?\s*"
    r"(?:现在呢|然后呢|后来呢|还有呢|还有吗|最新的呢|继续|接着说|再说说|"
    r"真的吗|为什么|怎么说|展开|详细说|多说点|怎么样了|现在怎么样)"
    r"[?？。！!～~\s]*$"
)
_FOLLOWUP_MAX_CHARS = 12

_SCRIPT_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")

_PRIVATE_HOST_SUFFIXES = (".local", ".internal", ".localhost", ".home", ".lan")
_BLOCKED_SCHEMES = frozenset({"file", "ftp", "gopher", "data", "javascript"})


# 热点浏览：问"最近有什么大事/热点/新鲜事"这类，要的是热榜而非具体事件检索
_HOT_BROWSING_RE = re.compile(
    r"(?:最近|近期|今天|这几天|这两天|现在|有|)?"
    r"(?:什么|哪些|啥)?"
    r"(?:大事|热点|热搜|新鲜事|新闻热点|热门(?:话题|新闻|事件)?|大家(?:在)?(?:讨论|聊|关注))"
)
# 具体事件线索：出现具体主体/事件名词时，应走关键词检索而非热榜
_SPECIFIC_HINT_RE = re.compile(r"事件|案件|事故|通报|进展|怎么样|怎么回事|详情|具体")


def has_freshness_intent(text: str) -> bool:
    """是否要求实时信息（最近/最新/今天…）。"""
    return bool(_FRESHNESS_RE.search(text or ""))


def looks_like_hot_browsing(text: str) -> bool:
    """是否"看看最近有什么热点"这类浏览型问题（要热榜，不是查具体事件）。

    命中热点词、且不含具体事件线索时才算——"湖南那个事件怎么样了"有具体
    指向，应走关键词检索，不能用热榜糊弄。
    """
    t = (text or "").strip()
    if not t or len(t) > 40:
        return False
    if _SPECIFIC_HINT_RE.search(t):
        return False
    return bool(_HOT_BROWSING_RE.search(t))


def looks_like_event_query(text: str) -> bool:
    """是否事件型查询（区别于普通列表型新闻查询）。

    只认明确事件名词。"最近项目进展"这类自指不在这里触发，否则会误联网。
    """
    return bool(_EVENT_NOUN_RE.search(text or ""))


def needs_web_search(text: str) -> bool:
    """是否需要联网取证。任一通道命中即联网。

    通道 1（原有）：新闻/事件名词 +（时效词 或 明确事件名词）。
    "最近的进展"这类自指不命中，交给 planner 或普通聊天处理。

    通道 2（新增）：时效词 + 求信息 + 外部主体。
    兜住"特朗普最近有什么动作""日本核污水排海最新情况"这类**有主体、有时效
    诉求，但用词不在名词表里**的问法。实测原判据对 13 条真实新闻问法只命中
    3 条，漏掉的全是这一类。

    通道 2 是安全网而非穷举：主判在 planner，这里宁可漏也不误判。
    """
    value = text or ""
    if _NEWS_NOUN_RE.search(value) and (
        has_freshness_intent(value) or looks_like_event_query(value)
    ):
        return True
    return bool(
        has_freshness_intent(value)
        and _INFO_SEEKING_RE.search(value)
        and _EXTERNAL_SUBJECT_RE.search(value)
    )


def looks_like_followup(text: str) -> bool:
    """是否指代式追问（"现在呢""后来呢"）——自身无主体，靠上一轮语境。"""
    value = (text or "").strip()
    if not value or len(value) > _FOLLOWUP_MAX_CHARS:
        return False
    return bool(_FOLLOWUP_RE.match(value))


def needs_web_search_after_followup(text: str, previous_user_text: str) -> bool:
    """追问是否应继承上一轮的检索需求。

    新闻是**会变的事实**：用户刚看完成果问"现在呢"，要的是新进展，不是把上
    一轮的报道复述一遍。实测这一问的 provider 为空，直接跳过了检索。
    只在上一轮确实查过新闻/热榜时继承，避免把普通闲聊的追问也拖去联网。
    """
    if not looks_like_followup(text):
        return False
    previous = previous_user_text or ""
    return bool(
        needs_web_search(previous) or looks_like_hot_browsing(previous)
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# 口语框架词：它们是"人和人说话"的包装，不是检索词。
# 实测同一事件："你知道最近湖南四岁幼童事件吗" 只命中 2 条，
# 去掉包装后同一引擎命中 52 条——搜索引擎会把整句当短语匹配。
_LEADING_FRAME_RE = re.compile(
    r"^(?:你(?:还)?知道|你(?:还)?记得|请问|麻烦|帮我(?:查一下|查查|查|搜一下|搜搜|搜|看看|看|找找|找)|"
    r"能不能|可不可以|可以帮我|我想(?:知道|了解|查)|有没有)"
)
_TRAILING_PARTICLE_RE = re.compile(r"[?？!！。，,~～\s]*(?:吗|呢|吧|啊|呀|么|嘛)?[?？!！。~～\s]*$")
# 浏览型问法包装："最近有什么…" / "今天有没有…"
# 注意不能带可选的"新/好"后缀：那会把"有什么新闻"的"新"吃掉，
# 剩下单字"闻"又触发过短回退，等于白洗。
_BROWSING_FRAME_RE = re.compile(
    r"^(?:最近|近期|今天|昨天|这几天|这两天)?(?:有什么|有啥|有没有|有哪些|哪些)"
)
# 裸的时间前缀：时间由 time_range 参数控制，留在检索词里只会稀释匹配
_LEADING_TIME_RE = re.compile(r"^(?:最近|近期|这几天|这两天|当前|现在)")


def clean_query(text: str) -> str:
    """把口语问句还原成检索词。

    只剥"包装"，不动实体词——"事件/新闻/幼童"这些必须保留，
    它们是检索的关键。剥完过短就退回原文，宁可多用几个词也不要搜空。
    """
    q = (text or "").strip()
    if not q:
        return ""
    original = q
    q = _LEADING_FRAME_RE.sub("", q)
    q = _BROWSING_FRAME_RE.sub("", q)
    q = _TRAILING_PARTICLE_RE.sub("", q)
    q = _LEADING_TIME_RE.sub("", q)
    q = q.strip()
    # 剥完太空（比如只剩"的新闻"）就退回原文
    return q if len(q) >= 2 else original


def _backend_url() -> str:
    from app.config import settings

    return str(getattr(settings, "search_backend_url", "") or "").strip().rstrip("/")


def configured() -> bool:
    """是否配置了检索后端。未配置时所有检索能力整体关闭。"""
    return bool(_backend_url())


def _is_safe_url(url: str) -> bool:
    """抓页 SSRF 护栏：只允许公网 http(s) 地址。"""
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(_PRIVATE_HOST_SUFFIXES):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True  # 域名：交由后续 DNS 解析，不做本地解析避免额外依赖
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
    )


# 新闻 URL 常见日期：/2026-09-10/ 与 /20260910A01/ 两类
_URL_DATE_DASHED_RE = re.compile(r"/(\d{4})-(\d{2})-(\d{2})(?:/|$|[^0-9])")
_URL_DATE_COMPACT_RE = re.compile(r"/(\d{4})(\d{2})(\d{2})(?:/|[^0-9])")


def _date_from_url(url: str) -> str:
    """从新闻 URL 路径提取发布日期；取不到返回空串。

    Bing News 等引擎不返回 publishedDate，但新闻站 URL 普遍带日期。
    这是**降级推断**而非权威时间，故调用方要保留 time_known 以便标注来源差别。
    """
    for pattern in (_URL_DATE_DASHED_RE, _URL_DATE_COMPACT_RE):
        match = pattern.search(url or "")
        if not match:
            continue
        year, month, day = (int(part) for part in match.groups())
        if not 2000 <= year <= 2100 or not 1 <= month <= 12 or not 1 <= day <= 31:
            continue
        return f"{year:04d}-{month:02d}-{day:02d}"
    return ""


def normalize_result(raw: dict[str, Any]) -> dict[str, Any] | None:
    """把后端结果规范化。

    来源是硬门槛：拿不到来源就不能进事实层（无法标注来源的报道不可引用）。
    时间不是硬门槛，但必须诚实标注——后端没给就从新闻 URL 推断，
    仍推断不出则留空并置 ``time_known=False``，由格式化层显式写成
    「时间未见标注」，避免把无日期内容当成近期新闻。
    """
    if not isinstance(raw, dict):
        return None
    url = str(raw.get("url") or "").strip()
    title = str(raw.get("title") or "").strip()
    if not url or not title:
        return None
    source = str(raw.get("source") or "").strip()
    if not source:
        source = urlparse(url).hostname or ""
    published = str(
        raw.get("publishedDate") or raw.get("published_at") or raw.get("published") or ""
    ).strip()
    time_known = bool(published)
    if not published:
        published = _date_from_url(url)
    if not source:
        return None
    return {
        "title": title[:300],
        "url": url[:1000],
        "source": source[:120],
        "published_at": published[:40],
        "time_known": time_known,
        "summary": str(raw.get("content") or raw.get("summary") or "").strip()[:1000],
        "language": str(raw.get("language") or "").strip()[:20],
    }


async def web_search(
    query: str,
    *,
    category: str = "general",
    time_range: str = "week",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """检索网页/新闻。后端未配置、超时或返回异常一律返回空列表。"""
    backend = _backend_url()
    text = (query or "").strip()
    if not backend or not text:
        return []

    from app.config import settings

    params = {
        "q": text[:400],
        "format": "json",
        "language": "zh-CN",
    }
    # time_range 传空串 = 不限时间窗。事件报道常年躺在索引里，
    # 硬限"最近一周"会把它们整批排除（实测同一事件 8 条 → 52 条）。
    if time_range:
        params["time_range"] = time_range if time_range in TIME_RANGES else "week"
    if category and category != "general":
        params["categories"] = category
    timeout = float(getattr(settings, "search_timeout", 15.0))
    cap = max(1, min(int(limit), int(getattr(settings, "search_max_results", 10))))

    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(f"{backend}/search", params=params)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("检索后端不可用（%s），本轮按未查到处理", type(exc).__name__)
        return []

    raw_items = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(raw_items, list):
        return []
    out: list[dict[str, Any]] = []
    for raw in raw_items:
        item = normalize_result(raw)
        if item is not None:
            out.append(item)
        if len(out) >= cap:
            break
    return out


async def fetch_page(url: str) -> dict[str, Any] | None:
    """抓取页面并抽取正文文本；护栏不通过或失败返回 None。"""
    if not configured() or not _is_safe_url(url):
        return None

    from app.config import settings

    timeout = float(getattr(settings, "search_timeout", 15.0))
    max_bytes = int(getattr(settings, "search_max_page_bytes", 500_000))
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            if "html" not in response.headers.get("content-type", "").lower():
                return None
            body = response.text[:max_bytes]
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.warning("抓页失败（%s）", type(exc).__name__)
        return None
    text = extract_text(body)
    if not text:
        return None
    return {"url": url, "text": text, "fetched_at": _now_iso()}


def extract_text(markup: str) -> str:
    """HTML → 纯文本（无第三方解析依赖）。"""
    body = _SCRIPT_RE.sub(" ", markup or "")
    body = _TAG_RE.sub("\n", body)
    body = html.unescape(body)
    body = _WS_RE.sub(" ", body)
    body = _NL_RE.sub("\n\n", body)
    return body.strip()[:8000]


def _title_features(title: str) -> set[str]:
    """标题特征：中文 2-gram + 英文/数字词，用于相似度比较。"""
    value = (title or "").strip()
    if not value:
        return set()
    features = {value[i : i + 2] for i in range(len(value) - 1) if value[i].strip()}
    features.update(token.lower() for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{1,}", value))
    return features


def _parse_time(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _similar(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def cluster_events(
    articles: list[dict[str, Any]],
    *,
    similarity: float = 0.34,
    window_days: int = 7,
) -> list[dict[str, Any]]:
    """把同一事件的多篇报道归并；返回按首报时间排序的事件列表。

    判据：标题相似度达阈值 + 发布时间相近。两者都不满足才算不同事件，
    避免把"同一地点的两起不同事件"错误合并。
    """
    items = [item for item in (articles or []) if isinstance(item, dict)]
    groups: list[dict[str, Any]] = []
    for item in items:
        features = _title_features(str(item.get("title") or ""))
        published = _parse_time(str(item.get("published_at") or ""))
        placed = False
        for group in groups:
            if _similar(features, group["_features"]) < similarity:
                continue
            other = group["_published"]
            if published is not None and other is not None:
                if abs((published - other).days) > window_days:
                    continue
            group["reports"].append(item)
            group["_features"] |= features
            if published is not None and (other is None or published < other):
                group["_published"] = published
                group["published_at"] = item.get("published_at", "")
                group["title"] = item.get("title", group["title"])
            placed = True
            break
        if not placed:
            groups.append({
                "title": item.get("title", ""),
                "published_at": item.get("published_at", ""),
                "reports": [item],
                "_features": features,
                "_published": published,
            })

    events = []
    for group in groups:
        sources = []
        for report in group["reports"]:
            name = str(report.get("source") or "")
            if name and name not in sources:
                sources.append(name)
        events.append({
            "title": group["title"],
            "published_at": group["published_at"],
            "sources": sources,
            "report_count": len(group["reports"]),
            "reports": group["reports"],
        })
    events.sort(key=lambda event: str(event.get("published_at") or ""))
    return events


def time_range_default() -> str:
    """默认检索时间窗（配置项，非法值回退 week）。"""
    from app.config import settings

    value = str(getattr(settings, "search_time_range", "week") or "week").strip()
    return value if value in TIME_RANGES else "week"


def format_events(events: list[dict[str, Any]], limit: int = 5) -> str:
    """事件聚合结果 → 文本（同一事件的多篇报道合成一条，附来源）。"""
    lines = []
    for event in (events or [])[:limit]:
        title = str(event.get("title") or "").strip()
        if not title:
            continue
        published = str(event.get("published_at") or "").strip()
        sources = [str(name) for name in (event.get("sources") or []) if str(name).strip()]
        count = int(event.get("report_count") or 1)
        line = f"事件：{title}"
        if published:
            line += f"（首次报道 {published}）"
        if count > 1:
            line += f"，共 {count} 篇报道"
        if sources:
            line += f"，来源：{'、'.join(sources[:5])}"
        lines.append(line)
    return "\n".join(lines)


def format_sources(results: list[dict[str, Any]], limit: int = 8) -> str:
    """格式化为带来源与时间的参考资料块（供 prompt 注入）。

    时间分三种写法，避免把推断时间或无日期内容当成权威事实：
    - 后端给了发布时间 → 直接标注；
    - 时间由链接推断 → 标注「据链接推断」；
    - 完全拿不到时间 → 写「时间未见标注」，不得据此声称"最新"。
    """
    lines = []
    for item in (results or [])[:limit]:
        title = str(item.get("title") or "").strip()
        source = str(item.get("source") or "").strip()
        published = str(item.get("published_at") or "").strip()
        if not title or not source:
            continue
        time_known = item.get("time_known", True)
        if not published:
            stamp = "时间未见标注"
        elif time_known:
            stamp = published
        else:
            stamp = f"{published}（据链接推断）"
        summary = str(item.get("summary") or "").strip()[:200]
        line = f"- {title}（{source}，{stamp}）"
        if summary:
            line += f"\n  {summary}"
        lines.append(line)
    return "\n".join(lines)


# 时间窗放宽顺序：空结果时退到更宽的窗口
_WIDER_RANGE = {"day": "week", "week": "month", "month": "year", "year": "year"}


def widen_time_range(time_range: str) -> str:
    return _WIDER_RANGE.get(time_range if time_range in TIME_RANGES else "week", "month")


def _min_results() -> int:
    """达标线：低于此结果数视为证据不足，继续放宽。"""
    from app.config import settings

    try:
        return max(1, int(getattr(settings, "search_min_results", 3)))
    except (TypeError, ValueError):
        return 3


# 事件深挖：为核心查询补几组正交角度，避免只搜"证实主流叙事"的词。
# 这次教训——"勒颈""后退"都藏在没主动搜的方向里，靠用户追问才补上。
_ANGLE_SUFFIXES = (
    "监控 经过 完整",      # 过程细节：还原到底怎么发生的
    "双方 说法 争议",      # 各方主张：谁说了什么
    "反转 后续 进展",      # 后续/反转：推翻当前印象的信息
    "律师 定性 分析",      # 定性争议：专业视角
)


def build_angle_queries(cleaned: str, limit: int = 3) -> list[str]:
    """给事件核查生成正交角度查询（主查询之外，主动找可能推翻印象的信息）。"""
    base = (cleaned or "").strip()
    if not base:
        return []
    # 取事件主体（去掉太长的部分，避免叠加后过长）
    core = base[:24]
    return [f"{core} {suffix}" for suffix in _ANGLE_SUFFIXES[:limit]]


async def search_and_cluster(
    query: str,
    *,
    time_range: str = "week",
    limit: int = 10,
    alt_query: str | None = None,
    deep_dive: bool = False,
) -> dict[str, Any]:
    """检索并按事件聚合；命中不足时逐级放宽。

    三级放宽（任一级达标即停，正常情况只跑第一级）：
      1. 清洗后的检索词 + news 类 + 配置时间窗
      2. 同上但去掉时间窗   ← 事件报道常年躺在索引里，硬限"最近一周"会整批排除
      3. 通用网页类 + 无时间窗 ← 覆盖非新闻站的报道
    另可传 ``alt_query``（如 planner 给出的规范化查询）作为备用检索词。

    达标线用 ``search_min_results``：旧逻辑只在**零结果**时兜底，
    实测整句问法只回 2 条、2 > 0 因而从不放宽，用户看到"只有一条转载稿"，
    而同一事件实际有 52 条。
    """
    cleaned = clean_query(query)
    alt = clean_query(alt_query) if alt_query else ""
    candidates = [cleaned] + ([alt] if alt and alt != cleaned else [])

    # 阶梯式放宽：命中不足就逐级松绑，而不是只在"零结果"时兜底一次。
    # 实测教训：整句问法只回 2 条，2 > 0 所以旧逻辑一次都没放宽，
    # 用户看到的是"只有一条转载稿"——而同一事件实际有 52 条。
    attempts: list[tuple[str, str, str]] = []
    for q in candidates:
        attempts.append((q, "news", time_range))
    attempts.append((cleaned, "news", ""))          # 去掉时间窗
    attempts.append((cleaned, "general", ""))       # 通用网页类
    for q in candidates[1:]:
        attempts.append((q, "general", ""))

    threshold = _min_results()
    results: list[dict[str, Any]] = []
    first = (candidates[0], "news", time_range)
    used: tuple[str, str, str] | None = None
    tried: set[tuple[str, str, str]] = set()
    for q, category, window in attempts:
        key = (q, category, window)
        if not q or key in tried:
            continue
        tried.add(key)
        results = await web_search(q, category=category, time_range=window, limit=limit)
        if len(results) >= threshold:
            used = key
            break

    # 深挖：事件/道德类问题追加正交角度检索，主动找可能推翻当前印象的信息
    # （过程细节/各方说法/后续反转/定性争议）。并入去重，不影响主结果。
    angle_added = 0
    if deep_dive and cleaned:
        angles = build_angle_queries(cleaned)
        if angles:
            # 角度查询彼此正交、互不依赖，并行发起。串行时实测深挖 6.6s
            # （多次放宽尝试 + 各角度全部排队），而热榜在同目录早就用了
            # asyncio.gather。return_exceptions 保证单个角度失败不牵连整体——
            # 深挖只是加料，绝不能让它把主结果一起弄丢。
            gathered = await asyncio.gather(
                *(
                    web_search(aq, category="general", time_range="", limit=limit)
                    for aq in angles
                ),
                return_exceptions=True,
            )
            seen_urls = {r.get("url") for r in results}
            for extra in gathered:
                if not isinstance(extra, list):
                    continue
                for it in extra:
                    if it.get("url") not in seen_urls:
                        seen_urls.add(it.get("url"))
                        results.append(it)
                        angle_added += 1

    # 第二检索源 GDELT（走代理）：并入结果、按 URL 去重。
    # GDELT 失败/限流返回空，不影响 SearXNG 已有结果。
    gdelt_count = 0
    try:
        from app.chat import gdelt_provider

        if gdelt_provider.configured():
            gdelt_items = await gdelt_provider.search(
                cleaned, time_range=time_range, limit=limit
            )
            if gdelt_items:
                seen_urls = {r.get("url") for r in results}
                added = [it for it in gdelt_items if it.get("url") not in seen_urls]
                gdelt_count = len(added)
                results = results + added
    except Exception as exc:  # noqa: BLE001 - 第二源绝不能拖垮主检索
        logger.warning("GDELT 合并失败（%s），仅用主检索结果", type(exc).__name__)

    events = cluster_events(results) if results else []
    return {
        "query": query,
        "query_used": (used or first)[0],
        "has_sources": bool(results),
        "result_count": len(results),
        # 分开记录两种降级，混在一起无法判断该调哪一处：
        # query_cleaned = 检索词被剥过口语包装；fallback_used = 放宽过类别/时间窗
        "query_cleaned": cleaned != (query or "").strip(),
        "fallback_used": (used or first) != first,
        "attempts": len(tried),
        "gdelt_count": gdelt_count,
        "angle_added": angle_added,
        "results": results,
        "events": events,
        "observed_at": _now_iso(),
    }


async def _demo() -> None:  # pragma: no cover - 手工排障入口
    data = await search_and_cluster("测试")
    print(len(data["results"]), len(data["events"]))


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_demo())


# 兼容旧调用方可能传入的同步上下文
def remaining_days(published_at: str, days: int = 7) -> int | None:
    """发布时间距现在的天数；解析失败返回 None。"""
    parsed = _parse_time(published_at)
    if parsed is None:
        return None
    delta = datetime.now(timezone.utc) - parsed
    return max(0, delta.days) if delta <= timedelta(days=days) else None
