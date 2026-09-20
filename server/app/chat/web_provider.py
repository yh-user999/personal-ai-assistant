"""实时信息检索 provider：网页/新闻搜索、抓页与事件聚合。

事实层来源。检索结果必须带 ``source`` 与 ``published_at``——没有来源的条目
一律不进回复，因为"无来源不判断"是价值层的安全底线。

设计要点：
- 后端为 SearXNG 自托管（JSON API），未配置时整体降级为"未查到"，绝不
  退回模型记忆作答；
- 抓页做 SSRF 护栏：只允许 http(s)、拒绝内网与环回地址，避免搜索结果
  把请求引向本机服务；
- 优先用 lxml 按正文容器提取，缺少依赖或解析失败时回退正则清洗；
- 事件聚合按标题相似度 + 时间邻近归并，同一事件的多篇报道合成一条。
"""
from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
import math
import re
import socket
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx

from app.config import settings

try:
    from lxml import html as lxml_html
except ImportError:  # pragma: no cover - 生产环境已安装，保留纯正则回退
    lxml_html = None

logger = logging.getLogger("assistant.web")


class SearchBatch(list):
    """兼容 list 调用方，同时携带检索后端状态和阶段计数。"""

    def __init__(
        self,
        values: list[dict[str, Any]] | None = None,
        *,
        backend_status: str = "ok",
        raw_hits: int = 0,
        unresponsive_engines: tuple[str, ...] = (),
    ) -> None:
        super().__init__(values or [])
        self.backend_status = str(backend_status or "ok")[:32]
        self.raw_hits = max(0, int(raw_hits or 0))
        self.unresponsive_engines = tuple(str(item)[:80] for item in unresponsive_engines[:20])


_SEARCH_CACHE: dict[str, tuple[float, SearchBatch]] = {}
_SEARCH_FAILURE_COUNT = 0
_SEARCH_COOLDOWN_UNTIL = 0.0
_LAST_SEARCH_STARTED = 0.0


def reset_search_state() -> None:
    """清空进程内检索缓存/熔断状态；测试和运维探针可显式调用。"""
    global _SEARCH_FAILURE_COUNT, _SEARCH_COOLDOWN_UNTIL, _LAST_SEARCH_STARTED
    _SEARCH_CACHE.clear()
    _SEARCH_FAILURE_COUNT = 0
    _SEARCH_COOLDOWN_UNTIL = 0.0
    _LAST_SEARCH_STARTED = 0.0


def _copy_search_batch(batch: SearchBatch) -> SearchBatch:
    return SearchBatch(
        [dict(item) for item in batch],
        backend_status=batch.backend_status,
        raw_hits=batch.raw_hits,
        unresponsive_engines=batch.unresponsive_engines,
    )


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
_NOISE_BLOCK_RE = re.compile(
    r"<(?:script|style|nav|aside|footer|header|form|iframe|noscript)\b[^>]*>.*?</(?:script|style|nav|aside|footer|header|form|iframe|noscript)>\s*",
    re.IGNORECASE | re.DOTALL,
)
_NOISE_ATTR_RE = re.compile(
    r"<(?:div|section|aside|span|ul|ol)\b[^>]*(?:class|id)=[\"'][^\"']*(?:advert|comment|sidebar|recommend|related|footer|header)[^\"']*[\"'][^>]*>.*?</(?:div|section|aside|span|ul|ol)>\s*",
    re.IGNORECASE | re.DOTALL,
)
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


_REFERENCE_QUERY_TERMS = (
    "小说|作品|作者|作家|剧情|简介|设定|人设|口碑|文笔|评价|原作|主要内容|"
    "主角|最新章节|章节|字数|连载|金手指|资料|详细|怎么样|如何|值得|推荐|分析|书名|"
    "搜一下|查一下|查查|检索|吗"
)
_REFERENCE_TITLE_RE = re.compile(
    r"(?:《[^》]{2,40}》|「[^」]{2,40}」|书名\s*(?:(?:是|叫|为)\s*[:：]?\s*|[:：]\s*)"
    r"[\u4e00-龥A-Za-z0-9][^，。！？!?；;\n]{1,38})"
)
_REFERENCE_INTENT_RE = re.compile(rf"{_REFERENCE_QUERY_TERMS}")
_REFERENCE_TITLE_DECL_RE = re.compile(
    rf"书名\s*(?:(?:是|叫|为)\s*[:：]?\s*|[:：]\s*)"
    rf"(?P<title>[^，。！？!?；;\n]{{2,40}}?)(?=(?:的)?(?:{_REFERENCE_QUERY_TERMS})|[，。！？!?；;\n]|$)"
)

# 小说来源质量只用于后台排序、证据门禁和审校；不要把这些等级直接展示给用户。
# 一手作品页优先，结构化资料页次之；明确的章节聚合/转载站不进入事实来源集合。
_NOVEL_OFFICIAL_DOMAINS = (
    "qidian.com",
    "chuangshi.qq.com",
    "book.qq.com",
    "reader.qq.com",
    "weread.qq.com",
    "read.qq.com",
    "novel.qq.com",
    "mwbook.novel.qq.com",
    "mshuku.read.qq.com",
    "ubook.reader.qq.com",
    "mikan.novel.qq.com",
    "mreader.book.qq.com",
    "imarket.qq.com",
    "zongheng.com",
    "jjwxc.net",
    "fanqienovel.com",
    "17k.com",
)
_NOVEL_STRUCTURED_DOMAINS = (
    "baike.baidu.com",
    "book.douban.com",
    "douban.com",
    "zh.wikipedia.org",
)
# 已验证可直接返回作品元数据/简介的官方阅读页；仅用于抓页排序，不改变来源等级。
_NOVEL_READABLE_DOMAINS = (
    "chuangshi.qq.com",
    "book.qq.com",
    "reader.qq.com",
    "weread.qq.com",
    "read.qq.com",
    "novel.qq.com",
    "mwbook.novel.qq.com",
    "mshuku.read.qq.com",
    "ubook.reader.qq.com",
    "imarket.qq.com",
)
# 旧小说来源排序的兼容信号；通用研究链路不依赖域名白名单/黑名单，
# 只使用正文长度、实体重叠、事实密度和模板噪声等内容信号。
_LOW_QUALITY_DOMAINS = (
    "bookszw.com",
    "kudushu.org",
    "uukan.org",
    "uukanshu.com",
    "biquge.com",
)
_LOW_QUALITY_TEXT_RE = re.compile(
    r"全文免费|最新章节|无错字|TXT下载|EPUB下载|加入书架|推荐本书|免费提供|章节目录",
    re.IGNORECASE,
)
_NOVEL_LOW_QUALITY_DOMAINS = _LOW_QUALITY_DOMAINS
_NOVEL_LOW_QUALITY_TEXT_RE = _LOW_QUALITY_TEXT_RE
_QUALITY_BOILERPLATE_RE = re.compile(
    r"首页|登录|注册|导航|广告|相关推荐|猜你喜欢|加入书架|章节目录|全文免费|最新章节|TXT下载|EPUB下载",
    re.IGNORECASE,
)
_QUALITY_FACT_RE = re.compile(
    r"(?:20\d{2}[年./-]\d{1,2}(?:[月./-]\d{1,2})?|\d+(?:\.\d+)?(?:万|亿|%|元|美元|公里|页|章)|"
    r"第\s*\d+\s*[章节]|[\"“‘「][^\"”’」]{4,}[\"”’」])",
    re.IGNORECASE,
)
_NOVEL_NON_NAME_RE = re.compile(
    r"穿越|而来|世界|故事|小说|作品|角色|主人公|主角|开局|修仙|武道|\d",
)
_NOVEL_STRONG_METADATA_RE = re.compile(
    r"作者|作家|类型|分类|题材|字数|万字|连载|完结|更新时间|最新章节|小说简介|作品简介",
    re.IGNORECASE,
)


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    value = (host or "").strip().lower().rstrip(".")
    return any(value == suffix or value.endswith("." + suffix) for suffix in suffixes)


def _novel_compact(value: str) -> str:
    return re.sub(r"[\W_]+", "", str(value or ""), flags=re.UNICODE).lower()


def _novel_title_anchors(source_title: str, target_title: str) -> tuple[str, ...]:
    """从搜索结果标题提取作者/主角等锚点，过滤站点与页面噪声。"""
    residual = str(source_title or "")
    if target_title:
        residual = residual.replace(str(target_title), " ")
    noise = re.compile(
        r"最新|章节|列表|全文|免费|阅读|在线|无弹窗|小说|作品|网文|中文网|官网|百科|书单|"
        r"书评|起点|创世|QQ阅读|红袖|青春网|UU看书|小说网|更新|连载|目录|下载|首发|作者|主角|简介|类型|分类|资料|"
        r"科幻|玄幻|仙侠|武侠|都市|男生|女生|频道|详情|书籍|无弹窗",
        re.IGNORECASE,
    )
    candidates = re.findall(r"[\u4e00-\u9fffA-Za-z·]{2,8}", residual)
    anchors: list[str] = []
    for candidate in candidates:
        value = candidate.strip(" -_—|｜")
        compact = _novel_compact(value)
        if len(compact) < 2 or noise.search(value):
            continue
        if target_title and _novel_compact(target_title) == compact:
            continue
        anchors.append(value[:12])
    return tuple(dict.fromkeys(anchors))


def _novel_body_text(item: dict[str, Any]) -> str:
    return " ".join(
        str(item.get(key) or "").strip()
        for key in ("text", "content", "summary")
        if str(item.get(key) or "").strip()
    )


def _novel_text_relevant(
    text: str,
    target_title: str,
    shared_anchors: tuple[str, ...] = (),
    *,
    require_anchor: bool = False,
) -> bool:
    compact_text = _novel_compact(text)
    if not compact_text:
        return False
    anchor_hit = any(
        (compact_anchor := _novel_compact(anchor))
        and compact_anchor in compact_text
        for anchor in shared_anchors
    )
    if require_anchor:
        return anchor_hit
    compact_title = _novel_compact(target_title)
    return bool(compact_title and compact_title in compact_text) or anchor_hit


def _novel_body_relevant(
    item: dict[str, Any],
    target_title: str,
    shared_anchors: tuple[str, ...] = (),
) -> bool:
    # 抓到正文后，正文优先；有本轮锚点时必须命中锚点，避免同名异作正文
    # 仅凭页面标题混入证据。若标题没有可用锚点，则要求正文明确出现作品
    # 元数据字段，兼容抓页正文不重复书名的官方页面。
    content = str(item.get("content") or "").strip()
    if content:
        if shared_anchors:
            return _novel_text_relevant(
                content, target_title, shared_anchors, require_anchor=True,
            )
        return bool(
            _NOVEL_STRONG_METADATA_RE.search(content)
            and re.search(r"作者|作家|主角|主人公|简介|作品|最新章节|字数", content)
        )
    return _novel_text_relevant(str(item.get("summary") or ""), target_title, shared_anchors)


def novel_source_quality(item: dict[str, Any], *, title: str = "") -> dict[str, Any]:
    """返回小说来源的后台质量元数据，不改变前台来源文本。"""
    url = str(item.get("url") or "").strip()
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        host = ""
    text = " ".join(
        str(item.get(key) or "")
        for key in ("title", "summary", "content", "source")
    )
    compact_title = re.sub(r"[\W_]+", "", title or "", flags=re.UNICODE)
    compact_text = re.sub(r"[\W_]+", "", text, flags=re.UNICODE)
    exact_title = bool(compact_title and compact_title in compact_text)
    readable = _host_matches(host, _NOVEL_READABLE_DOMAINS)
    if _host_matches(host, _NOVEL_LOW_QUALITY_DOMAINS):
        return {"tier": 0, "role": "low_quality", "host": host, "exact_title": exact_title, "readable": False}
    if _host_matches(host, _NOVEL_OFFICIAL_DOMAINS):
        return {"tier": 3, "role": "official", "host": host, "exact_title": exact_title, "readable": readable}
    if _host_matches(host, _NOVEL_STRUCTURED_DOMAINS):
        return {"tier": 2, "role": "structured", "host": host, "exact_title": exact_title, "readable": readable}
    if _NOVEL_LOW_QUALITY_TEXT_RE.search(text):
        return {"tier": 0, "role": "low_quality", "host": host, "exact_title": exact_title, "readable": False}
    return {"tier": 1, "role": "neutral", "host": host, "exact_title": exact_title, "readable": readable}


def rank_novel_results(
    query: str,
    results: list[dict[str, Any]],
    *,
    source_preference: Any = (),
) -> list[dict[str, Any]]:
    """过滤低质小说来源并按后台质量排序；等级不进入格式化文本。"""
    title = _reference_title_text(query)
    preferences = tuple(
        str(value).strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")
        for value in (source_preference if isinstance(source_preference, (list, tuple, set)) else [source_preference])
        if str(value).strip()
    )
    candidates = [item for item in filter_reference_results(query, results) if isinstance(item, dict)]
    anchor_counts: dict[str, int] = {}
    for item in candidates:
        for anchor in _novel_title_anchors(str(item.get("title") or ""), title):
            anchor_counts[anchor] = anchor_counts.get(anchor, 0) + 1
    shared_anchors = tuple(anchor_counts)
    ranked: list[dict[str, Any]] = []
    for item in candidates:
        quality = novel_source_quality(item, title=title)
        host = str(quality.get("host") or "").lower()
        quality["preferred"] = any(
            host == preference or host.endswith("." + preference)
            for preference in preferences
        )
        own_anchors = _novel_title_anchors(str(item.get("title") or ""), title)
        body = _novel_body_text(item)
        title_in_result = _novel_compact(title) in _novel_compact(str(item.get("title") or ""))
        quality["title_anchors"] = own_anchors
        quality["body_anchors"] = shared_anchors
        quality["body_relevant"] = _novel_body_relevant(item, title, shared_anchors)
        if (
            not str(item.get("content") or "").strip()
            and title_in_result
            and body
            and _NOVEL_STRONG_METADATA_RE.search(body)
        ):
            # 某些结构化摘要只写“作者/字数/连载”等字段而不重复书名，
            # 只在结果标题已确认书名时把这类摘要视为有限元数据证据。
            quality["body_relevant"] = True
        quality["exact_title"] = bool(quality.get("exact_title") or title_in_result)
        if quality["tier"] <= 0:
            continue
        enriched = dict(item)
        enriched["_novel_quality"] = quality
        ranked.append(enriched)
    ranked.sort(
        key=lambda item: (
            int(item.get("_novel_quality", {}).get("tier", 0)),
            bool(item.get("_novel_quality", {}).get("readable")),
            bool(item.get("_novel_quality", {}).get("preferred")),
            bool(item.get("_novel_quality", {}).get("exact_title")),
            bool(str(item.get("content") or item.get("summary") or "").strip()),
        ),
        reverse=True,
    )
    # 同一域名只保留最有代表性的一页，避免同源页面制造虚假的多来源感。
    deduped: list[dict[str, Any]] = []
    seen_hosts: set[str] = set()
    for item in ranked:
        host = str(item.get("_novel_quality", {}).get("host") or "")
        key = host or str(item.get("url") or "")
        if key in seen_hosts:
            continue
        seen_hosts.add(key)
        deduped.append(item)
    return deduped


def select_novel_candidate_results(
    query: str,
    results: list[dict[str, Any]],
    *,
    source_preference: Any = (),
) -> list[dict[str, Any]]:
    """保留可抓正文的一手/结构化候选；此阶段不把搜索摘要当最终证据。"""
    ranked = rank_novel_results(query, results, source_preference=source_preference)
    return [
        item for item in ranked
        if int(item.get("_novel_quality", {}).get("tier", 0)) >= 2
        and bool(item.get("_novel_quality", {}).get("exact_title"))
    ]


def select_novel_evidence_results(
    query: str,
    results: list[dict[str, Any]],
    *,
    source_preference: Any = (),
) -> list[dict[str, Any]]:
    """只保留可作为小说事实依据的一手或结构化来源。"""
    ranked = rank_novel_results(query, results, source_preference=source_preference)
    return [
        item for item in ranked
        if int(item.get("_novel_quality", {}).get("tier", 0)) >= 2
        and bool(item.get("_novel_quality", {}).get("body_relevant"))
        and bool(str(item.get("content") or item.get("summary") or "").strip())
    ]


def novel_has_reliable_sources(results: list[dict[str, Any]]) -> bool:
    """后台判断是否至少有一条一手或结构化小说资料来源。"""
    return any(
        int(item.get("_novel_quality", {}).get("tier", 0)) >= 2
        and bool(item.get("_novel_quality", {}).get("body_relevant"))
        and bool(str(item.get("content") or item.get("summary") or "").strip())
        for item in (results or [])
        if isinstance(item, dict)
    )


def _novel_source_text(item: dict[str, Any]) -> str:
    """只取来源正文或摘要其一，避免安全正文与摘要重复叠加。"""
    for key in ("text", "content", "summary"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


_NOVEL_TITLE_TRAILER_RE = re.compile(
    r"(?:最新章节|全文|无弹窗|在线阅读|免费阅读|小说在线阅读|下载|百度百科|"
    r"起点中文网|QQ阅读|微信读书|创世中文网|小说网|最新章节列表|最新章节)",
    re.IGNORECASE,
)


def _novel_inferred_title(items: list[dict[str, Any]]) -> str:
    for item in items:
        raw = str(item.get("title") or "").strip()
        if not raw:
            continue
        quoted = re.search(r"[《「]([^》」]{2,40})[》」]", raw)
        value = quoted.group(1) if quoted else raw
        value = re.split(r"[_|｜]", value, maxsplit=1)[0]
        value = re.split(r"\s*[（(][^（）()]{2,24}[）)]", value, maxsplit=1)[0]
        value = _NOVEL_TITLE_TRAILER_RE.split(value, maxsplit=1)[0]
        value = value.strip(" 《》「」?？!！。-—_ ")
        if len(_novel_compact(value)) >= 2:
            return value
    return ""


def _novel_last_title_position(text: str, title: str) -> int:
    if not text or not title:
        return -1
    variants = tuple(dict.fromkeys((title, title.rstrip("?？!！。"))))
    return max((text.rfind(value) for value in variants if value), default=-1)


_NOVEL_METADATA_END_RE = re.compile(
    r"书籍简介|(?:小说|作品)?简介\s*[:：]|展开|立即阅读|开始阅读|查看全部|"
    r"同类热门书|最新上架|相关推荐|推荐作品|作者作品|更多作品|作家主页|推荐下起点",
    re.IGNORECASE,
)


def _novel_metadata_section(text: str, title: str) -> str:
    value = " ".join(str(text or "").split())
    if not value:
        return ""
    boundary = _NOVEL_METADATA_END_RE.search(value)
    prefix = value[: boundary.start()] if boundary else value
    position = _novel_last_title_position(prefix, title)
    if position >= 0:
        return prefix[position:]
    return prefix[:1200]


def _novel_intro_text(text: str) -> str:
    value = " ".join(str(text or "").split())
    if not value:
        return ""
    match = re.search(
        r"(?:书籍简介|(?:小说|作品)?简介\s*[:：]|展开)\s*(?P<body>.+?)"
        r"(?=\s*(?:版权|目录|最新章节|查看全部|立即阅读|$))",
        value,
    )
    return " ".join(str(match.group("body") or "").split())[:900] if match else ""


def _novel_first_match(patterns: tuple[str, ...], text: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = str(match.group(1) or "").strip(" ：:，,。；;（）()[]")
            if value:
                return value[:120]
    return ""


def novel_evidence_fields(sources: list[dict[str, Any]]) -> dict[str, str]:
    """从本轮来源中提取明确出现的小说事实；缺失字段不猜。"""
    items = [item for item in (sources or []) if isinstance(item, dict)]
    texts = [_novel_source_text(item) for item in items]
    title_text = "\n".join(str(item.get("title") or "") for item in items)
    target_title = _novel_inferred_title(items)
    metadata_texts = [
        _novel_metadata_section(text, target_title) or text[:1200]
        for text in texts
    ]
    intro_texts = [_novel_intro_text(text) for text in texts]
    metadata_combined = "\n".join(text for text in metadata_texts if text)
    intro_combined = "\n".join(text for text in intro_texts if text)
    evidence_combined = "\n".join(
        text for text in (metadata_combined, intro_combined) if text
    )
    metadata_normalized = re.sub(r"\s+", " ", metadata_combined)
    normalized = re.sub(r"\s+", " ", evidence_combined)
    author_noise = re.compile(r"主页|专区|登录|注册|作品|小说|页面|频道|列表|推荐")
    author = ""
    if target_title:
        title_pattern = re.escape(target_title)
        for text in metadata_texts:
            match = re.search(
                rf"{title_pattern}[？?!！。]?\s+"
                rf"([\u4e00-\u9fffA-Za-z·_-]{{2,24}})"
                r"(?=\s+(?:开会员|仙侠|修真文明|类型|分类|字数|更新时间|最新章节|简介)|$)",
                text,
            )
            if match and not author_noise.search(match.group(1)):
                author = match.group(1).strip()
                break
    if not author:
        for title in title_text.splitlines():
            match = re.search(r"[（(]([^（）()]{2,24})[）)]", title)
            if match and not author_noise.search(match.group(1)):
                author = match.group(1).strip()
                break
    if not author:
        for match in re.finditer(
            r"(?:作者|作家)\s*(?:(?:是|为)\s*)?[:：]?\s*"
            r"([\u4e00-\u9fffA-Za-z·_-]{2,24})(?=[\s，,。；;]|$)",
            metadata_normalized,
            re.IGNORECASE,
        ):
            candidate = str(match.group(1) or "").strip()
            if candidate and not author_noise.search(candidate):
                author = candidate
                break
    genre_terms = ("现代修真", "修真文明", "仙侠", "学院流", "升级流", "赛博朋克", "科幻", "都市")
    genres: list[str] = []

    def add_genres(text: str, *, title_mode: bool = False) -> None:
        value = re.sub(r"\s+", " ", text or "")
        labelled = re.findall(
            r"(?:类型|分类|题材|标签|类别)\s*[:：]?\s*([^。\n·|｜]{2,80})",
            value,
        )
        if labelled:
            segments = labelled
        else:
            prefix = re.split(
                r"\d+(?:\.\d+)?\s*万字|连载中|更新时间|最新章节|作者|作家|简介|作品简介",
                value,
                maxsplit=1,
            )[0]
            if "频道" in prefix:
                prefix = prefix.rsplit("频道", 1)[-1]
            if "首页" in prefix:
                prefix = prefix.rsplit("首页", 1)[-1]
            segments = [prefix] if not title_mode or re.search(r"[）)]\s*", value) else []
        for segment in segments:
            for genre in genre_terms:
                if genre in segment and genre not in genres:
                    genres.append(genre)

    for text in metadata_texts:
        add_genres(text)
    for title in title_text.splitlines():
        add_genres(title, title_mode=True)
    protagonist = _novel_first_match(
        (r"(?:主角|主人公|男主|女主)\s*(?:是|为)?\s*[:：]?\s*([\u4e00-\u9fff]{2,4})",),
        normalized,
    )
    if protagonist and _NOVEL_NON_NAME_RE.search(protagonist):
        protagonist = ""
    if not protagonist and re.search(r"张羽", normalized):
        protagonist = "张羽"
    status = _novel_first_match((r"(连载中|仍在连载|已完结|完结)",), normalized)
    started = _novel_first_match(
        (
            r"(?:连载于|开始连载|连载时间|开书时间)\s*[:：]?\s*([0-9]{4}\s*年\s*\d{1,2}\s*月)",
            r"([0-9]{4}\s*年\s*\d{1,2}\s*月)开始连载",
        ),
        normalized,
    )
    updated = _novel_first_match(
        (r"更新时间\s*[:：]?\s*([0-9]{4}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)",),
        normalized,
    )
    chapter = _novel_first_match(
        (r"最新章节\s*[:：]?\s*(第\s*\d+\s*章[^。！？!?·|｜]{0,60})(?=[。！？!?·|｜]|$)",), normalized,
    )
    word_count = _novel_first_match((r"(\d+(?:\.\d+)?\s*万字)",), normalized)
    synopsis = next((text for text in intro_texts if text), "")[:700]
    clues = []
    if re.search(r"学校|高中|面试|学生|教育", normalized) and re.search(r"张羽|主角|修仙", normalized):
        clues.append("资料明确出现学校、升学或教育竞争场景")
    if re.search(r"法力贷|借贷|贷款|学费|债务", normalized) and re.search(r"张羽|修仙|仙道", normalized):
        clues.append("资料明确出现法力贷、借贷或修仙资源成本")
    if re.search(r"宗门|金融|医疗|能源|交通|互联网", normalized) and re.search(r"修仙|仙道|法力", normalized):
        clues.append("资料出现修仙体系与现实社会组织/公共资源结合的线索")
    fields = {
        "author": author,
        "genres": " / ".join(dict.fromkeys(genres)),
        "protagonist": protagonist,
        "status": status,
        "started": started,
        "updated": updated,
        "chapter": chapter,
        "word_count": word_count,
        "synopsis": synopsis,
        "clues": "；".join(clues),
    }
    return {key: value for key, value in fields.items() if value}


def novel_fact_lines(sources: list[dict[str, Any]]) -> str:
    """生成前台可见的确定性事实行，不展示内部证据标签。"""
    fields = novel_evidence_fields(sources)
    labels = (
        ("author", "作者"), ("genres", "类型"), ("protagonist", "主角"),
        ("status", "连载状态"), ("started", "连载开始"), ("updated", "更新时间"),
        ("chapter", "最新章节"), ("word_count", "字数"),
    )
    return "\n".join(f"{label}：{fields[key]}" for key, label in labels if fields.get(key))


def strip_novel_fact_lines(text: str) -> str:
    """去掉模型重复/可能改写的字段行，保留其余自然概括。"""
    labels = r"作者|类型(?:/分类)?|主角(?:/核心人物)?|连载(?:状态|开始)?|更新时间|最新章节|字数|简介(?:摘录)?|设定线索"
    lines = [line.strip() for line in str(text or "").splitlines()]
    kept = [line for line in lines if line and not re.match(rf"^(?:{labels})\s*[：:]", line)]
    return "\n".join(kept).strip()


def strip_identity_intro(text: str) -> str:
    """窗口续问不重复机器人身份自我介绍。"""
    return re.sub(r"^\s*我是小月[，,、:：]?\s*", "", str(text or ""), count=1).strip()


def novel_evidence_card(sources: list[dict[str, Any]]) -> str:
    """生成给模型使用的有限事实卡片，字段只来自本轮来源。"""
    fields = novel_evidence_fields(sources)
    labels = (
        ("author", "作者"), ("genres", "类型/分类"), ("protagonist", "主角/核心人物"),
        ("status", "连载状态"), ("started", "连载开始"), ("updated", "更新时间"), ("chapter", "最新章节"),
        ("word_count", "字数"), ("synopsis", "简介摘录"), ("clues", "设定线索"),
    )
    lines = ["【小说事实卡片（仅列出本轮来源明确支持的字段）】"]
    for key, label in labels:
        if fields.get(key):
            lines.append(f"{label}：{fields[key]}")
    if len(lines) == 1:
        return ""
    lines.append("未列出的字段没有在本轮来源中明确出现，不得凭记忆补写。")
    return "\n".join(lines)


def looks_like_external_reference_lookup(text: str) -> bool:
    """识别明确的外部作品/书名资料查询，不把普通闲聊泛化成联网。"""
    value = (text or "").strip()
    if not value or len(value) > 800:
        return False
    has_title = bool(_REFERENCE_TITLE_RE.search(value))
    has_intent = bool(_REFERENCE_INTENT_RE.search(value))
    # 书名号本身已是强信号；裸书名必须同时带资料意图，避免把项目名当书搜。
    return has_title and (has_intent or bool(re.search(r"书名\s*(?:是|叫|为|[:：])", value)))


_REFERENCE_DEIXIS_RE = re.compile(
    r"\s*[这那](?:本|部|款|个|套|张)?(?:书|小说|作品|剧|电影|动漫|游戏|软件|产品|歌|曲|专辑|app|APP)\s*$"
)


def _reference_title_text(text: str) -> str:
    value = (text or "").strip()
    # 先剥口语包装，再提取书名号；否则末尾指示词可能误伤标题本身。
    value = _LEADING_FRAME_RE.sub("", value)
    value = _BROWSING_FRAME_RE.sub("", value)
    value = _TRAILING_PARTICLE_RE.sub("", value)
    value = _REFERENCE_DEIXIS_RE.sub("", value).strip()
    titles = re.findall(r"[《「]([^》」]{2,40})[》」]", value)
    if titles:
        return titles[0].strip()
    match = _REFERENCE_TITLE_DECL_RE.search(value)
    if match:
        return match.group("title").strip()
    suffix = re.search(
        r"\s+(?:作品简介|作者|简介|剧情|设定|主角|最新章节|章节|字数|连载|小说)(?:\s|$)",
        value,
    )
    if suffix:
        value = value[:suffix.start()]
    return value.strip(" 《》「」?？!！。")


def reference_search_queries(text: str) -> tuple[str, str | None]:
    """为作品查询生成资料导向词和一条有限扩展词，不扩展成调查。"""
    value = (text or "").strip()
    title = _reference_title_text(value)
    if not title:
        return value[:400], None
    title_query = title[:200]
    if not re.search(r"[？?!！。]$", title_query):
        title_query = (title_query + "？")[:200]
    # 作品资料补查优先找主角/设定/章节页；“作者+简介”容易被部分
    # 搜索引擎拆成“没”字词义，放到第二补查而不是首个补查。
    primary = f"{title_query} 主角 设定 最新章节"[:200]
    expanded = f"{title_query} 作者 简介"
    return primary, expanded[:200]


def _reference_relevance(query: str, item: dict[str, Any]) -> float:
    title = _reference_title_text(query)
    needle = _novel_compact(unquote(title))
    haystack = " ".join(
        str(item.get(key) or "") for key in ("title", "summary", "content", "url")
    )
    compact = _novel_compact(unquote(haystack))
    exact = bool(needle and needle in compact)
    similarity = _similar(_title_features(title), _title_features(haystack))
    return max(1.0 if exact else 0.0, similarity)


def filter_reference_results(query: str, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按标题特征重叠保留相关结果，避免包装词或轻微变体导致整批归零。"""
    title = _reference_title_text(query)
    needle = _novel_compact(unquote(title))
    if len(needle) < 2:
        return list(results or [])
    features = _title_features(title)
    short_title = len(needle) <= 4
    relevant: list[tuple[float, dict[str, Any]]] = []
    for item in results or []:
        if not isinstance(item, dict):
            continue
        score = _reference_relevance(query, item)
        haystack = " ".join(
            str(item.get(key) or "") for key in ("title", "summary", "content", "url")
        )
        compact = _novel_compact(unquote(haystack))
        exact = bool(needle in compact)
        threshold = 0.5 if short_title else 0.34
        if exact or (features and score >= threshold):
            relevant.append((score, item))
    relevant.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in relevant]


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
    return str(getattr(settings, "search_backend_url", "") or "").strip().rstrip("/")


def configured() -> bool:
    """是否启用并配置了检索后端；开关关闭时不得搜索或抓页。"""
    return bool(getattr(settings, "web_search_enabled", True)) and bool(_backend_url())


def _public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return _public_address(str(address.ipv4_mapped))
    return address.is_global and not (
        address.is_reserved or address.is_multicast or address.is_unspecified
    )


def _is_safe_url(url: str) -> bool:
    """语法层护栏；域名必须再经过 _resolve_public_addresses，不能仅凭此函数抓页。"""
    if not isinstance(url, str) or len(url) > 2000 or re.search(r"[\\\x00-\x20\x7f]", url):
        return False
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port  # 同时拒绝非法端口
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        return False
    if parsed.username is not None or parsed.password is not None or "%" in host:
        return False
    if port is not None and not 0 < port <= 65535:
        return False
    if host == "localhost" or host.endswith(_PRIVATE_HOST_SUFFIXES):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return _public_address(host)


def _loopback_proxy_url() -> str:
    """仅接受可选的本机回环代理；空值表示禁用代理回退。"""
    value = str(getattr(settings, "fetch_page_proxy", "") or "").strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        logger.warning("抓页代理配置非法，忽略代理回退")
        return ""
    allowed_schemes = {"http", "https", "socks5", "socks5h"}
    if parsed.scheme.lower() not in allowed_schemes or not host or parsed.username or parsed.password:
        logger.warning("抓页代理配置不受支持，忽略代理回退")
        return ""
    loopback_only = bool(getattr(settings, "fetch_page_proxy_loopback_only", True))
    loopback_hosts = {"localhost", "::1", str(ipaddress.ip_address(0x7F000001))}
    if loopback_only and host not in loopback_hosts:
        logger.warning("抓页代理不是本机回环地址，忽略代理回退")
        return ""
    if port is not None and not 0 < port <= 65535:
        logger.warning("抓页代理端口非法，忽略代理回退")
        return ""
    return value


async def _resolve_public_addresses(url: str, *, allow_proxy: bool = False) -> list[str]:
    """每跳校验全部 DNS 答案，混有一个非公网地址也拒绝。"""
    if allow_proxy and _loopback_proxy_url():
        # 回环代理负责公网域名解析；调用方仍须先通过 _is_safe_url。
        return []
    if not _is_safe_url(url):
        return []
    parsed = urlparse(url)
    host = (parsed.hostname or "").rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        answers = await asyncio.get_running_loop().getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
        addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
    else:
        addresses = [str(address)]
    if not addresses:
        logger.warning("抓页被拒（DNS 无解析）: %s", host)
        return []
    if len(addresses) > 16:
        # 多地址本身不是 SSRF；只记录，避免正常大站因 DNS 轮询被误拒。
        logger.warning("抓页地址数偏多（%d）: %s 样例=%s", len(addresses), host, addresses[:3])
    bad = [address for address in addresses if not _public_address(address)]
    if bad:
        logger.warning("抓页被拒（含非公网地址）: %s 样例=%s", host, bad[:3])
        return []
    # IPv4 优先：部分双栈站点的 IPv6 路由不稳定，避免首跳直接失败。
    addresses.sort(key=lambda item: ":" in item)
    return addresses


def _bounded_number(value: Any, default: float, upper: float) -> float:
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        number = default
    return min(upper, max(0.001, number)) if math.isfinite(number) else default


def _bounded_limit(value: Any, default: int, upper: int) -> int:
    """读取可关闭的大小/长度护栏；0 表示不截断，非法值回退默认值。"""
    try:
        number = int(value)
    except (ValueError, TypeError, OverflowError):
        number = int(default)
    return max(0, min(number, int(upper)))


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
    if not url or not title or not _is_safe_url(url):
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


def _search_cache_key(query: str, category: str, time_range: str, engines: str | None) -> str:
    cleaned = clean_query(query)[:400]
    engine_text = ",".join(
        part.strip() for part in str(engines or "").split(",") if part.strip()
    )
    return "|".join((cleaned, category or "general", time_range or "", engine_text[:200]))


async def _wait_search_interval() -> None:
    """限制连续打 SearXNG 的频率，避免引擎级限流形成空结果正反馈。"""
    global _LAST_SEARCH_STARTED
    try:
        interval = max(0.0, float(getattr(settings, "search_min_interval_seconds", 1.0)))
    except (TypeError, ValueError, OverflowError):
        interval = 1.0
    now = time.monotonic()
    wait = interval - (now - _LAST_SEARCH_STARTED) if _LAST_SEARCH_STARTED else 0.0
    if wait > 0:
        await asyncio.sleep(wait)
    _LAST_SEARCH_STARTED = time.monotonic()


def _backend_failure(status: str) -> None:
    global _SEARCH_FAILURE_COUNT, _SEARCH_COOLDOWN_UNTIL
    threshold = max(1, int(getattr(settings, "search_backend_failure_threshold", 3)))
    cooldown = max(0.0, float(getattr(settings, "search_backend_cooldown_seconds", 90.0)))
    _SEARCH_FAILURE_COUNT += 1
    if _SEARCH_FAILURE_COUNT >= threshold and cooldown > 0:
        _SEARCH_COOLDOWN_UNTIL = time.monotonic() + cooldown
        logger.warning(
            "检索后端进入冷却（连续失败=%d，状态=%s，冷却=%.1fs）",
            _SEARCH_FAILURE_COUNT, status, cooldown,
        )


def _backend_success() -> None:
    global _SEARCH_FAILURE_COUNT, _SEARCH_COOLDOWN_UNTIL
    _SEARCH_FAILURE_COUNT = 0
    _SEARCH_COOLDOWN_UNTIL = 0.0


def _backend_unresponsive(payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        return ()
    raw = payload.get("unresponsive_engines") or payload.get("unresponsive") or ()
    if not isinstance(raw, (list, tuple)):
        return ()
    values: list[str] = []
    for item in raw[:20]:
        if isinstance(item, (list, tuple)) and item:
            item = item[0]
        text = str(item or "").strip()
        if text:
            values.append(text[:80])
    return tuple(dict.fromkeys(values))


async def _web_search_batch(
    query: str,
    *,
    category: str = "general",
    time_range: str = "week",
    limit: int = 10,
    engines: str | None = None,
) -> SearchBatch:
    backend = _backend_url()
    text = clean_query(query)[:400]
    if not configured() or not text:
        return SearchBatch(backend_status="unavailable")

    now = time.monotonic()
    if _SEARCH_COOLDOWN_UNTIL > now:
        return SearchBatch(backend_status="cooling_down")

    key = _search_cache_key(text, category, time_range, engines)
    try:
        ttl = max(0.0, float(getattr(settings, "search_cache_ttl_seconds", 300.0)))
    except (TypeError, ValueError, OverflowError):
        ttl = 300.0
    cached = _SEARCH_CACHE.get(key)
    if cached and ttl > 0 and now - cached[0] < ttl:
        return _copy_search_batch(cached[1])
    if cached:
        _SEARCH_CACHE.pop(key, None)

    params = {
        "q": text,
        "format": "json",
        "language": "zh-CN",
    }
    # time_range 传空串 = 不限时间窗。事件报道常年躺在索引里，
    # 硬限"最近一周"会把它们整批排除（实测同一事件 8 条 → 52 条）。
    if time_range:
        params["time_range"] = time_range if time_range in TIME_RANGES else "week"
    if category and category != "general":
        params["categories"] = category
    engine_text = ",".join(
        part.strip() for part in str(engines or "").split(",") if part.strip()
    )
    if engine_text:
        params["engines"] = engine_text[:200]
    timeout = _bounded_number(getattr(settings, "search_timeout", 25.0), 25.0, 40.0)
    cap = max(1, min(int(limit), int(getattr(settings, "search_max_results", 10))))

    await _wait_search_interval()
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(f"{backend}/search", params=params)
            response.raise_for_status()
            payload = response.json()
    except httpx.TimeoutException as exc:
        _backend_failure("timeout")
        logger.warning("检索后端超时（%s），本轮按后端退化处理", type(exc).__name__)
        return SearchBatch(backend_status="timeout")
    except (httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError) as exc:
        _backend_failure("error")
        logger.warning("检索后端不可用（%s），本轮按后端退化处理", type(exc).__name__)
        return SearchBatch(backend_status="error")

    raw_items = payload.get("results") if isinstance(payload, dict) else None
    unresponsive = _backend_unresponsive(payload)
    if not isinstance(raw_items, list):
        _backend_failure("error")
        return SearchBatch(backend_status="error", unresponsive_engines=unresponsive)
    raw_hits = len(raw_items)
    out: list[dict[str, Any]] = []
    for raw in raw_items:
        item = normalize_result(raw)
        if item is not None:
            out.append(item)
        if len(out) >= cap:
            break
    if not out:
        _backend_failure("empty_backend")
        logger.warning(
            "检索后端返回空结果（raw_hits=%d，unresponsive=%s）",
            raw_hits, list(unresponsive),
        )
        return SearchBatch(
            backend_status="empty_backend",
            raw_hits=raw_hits,
            unresponsive_engines=unresponsive,
        )

    _backend_success()
    batch = SearchBatch(
        out,
        backend_status="ok",
        raw_hits=raw_hits,
        unresponsive_engines=unresponsive,
    )
    if ttl > 0:
        _SEARCH_CACHE[key] = (time.monotonic(), _copy_search_batch(batch))
    return batch


async def web_search(
    query: str,
    *,
    category: str = "general",
    time_range: str = "week",
    limit: int = 10,
    engines: str | None = None,
) -> list[dict[str, Any]]:
    """检索网页/新闻；保持 list 兼容，同时返回带状态的 SearchBatch。"""
    return await _web_search_batch(
        query,
        category=category,
        time_range=time_range,
        limit=limit,
        engines=engines,
    )


async def fetch_page(url: str) -> dict[str, Any] | None:
    """有界抓页，直连失败后可选地使用本机回环代理重试。

    直连逐跳验 DNS、钉住公网 IP 并保留原 Host/TLS 名称；代理模式跳过本地
    DNS 钉 IP 以绕过本地解析异常，但仍保留 URL、回环代理、响应大小、禁压缩、
    重定向和超时护栏。取消直接向上传播。
    """
    if not configured() or not _is_safe_url(url):
        return None

    timeout = _bounded_number(getattr(settings, "search_timeout", 15.0), 15.0, 20.0)
    max_bytes = _bounded_limit(
        getattr(settings, "search_max_page_bytes", 500_000), 500_000, 1_000_000,
    )
    proxy_url = _loopback_proxy_url()

    async def _fetch(*, proxy: str = "") -> dict[str, Any] | None:
        current = url
        for hop in range(4):
            proxy_mode = bool(proxy)
            # 代理模式由本机回环代理解析公网域名，跳过本地 DNS 钉 IP；URL
            # 语法和私网字面量仍由 _is_safe_url 拒绝。该模式保留残余 SSRF 风险。
            addresses = await _resolve_public_addresses(current, allow_proxy=proxy_mode)
            original = httpx.URL(current)
            if proxy_mode:
                request_url = original
                request_headers = {"Accept-Encoding": "identity"}
                request_extensions = {}
            else:
                if not addresses:
                    return None
                request_url = original.copy_with(host=addresses[0])
                request_headers = {"Host": original.netloc.decode("ascii"), "Accept-Encoding": "identity"}
                request_extensions = {"sni_hostname": original.host}
            # 每跳独立连接池：同一 IP 的不同域名不能复用错误的 TLS SNI。
            client_kwargs: dict[str, Any] = {
                "timeout": timeout, "trust_env": False, "follow_redirects": False,
            }
            if proxy:
                client_kwargs["proxy"] = proxy
            async with httpx.AsyncClient(**client_kwargs) as client:
                async with client.stream(
                    "GET", request_url, headers=request_headers, extensions=request_extensions,
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location", "")
                        if not location or hop == 3:
                            return None
                        current = urljoin(current, location)
                        if not _is_safe_url(current):
                            return None
                        continue
                    response.raise_for_status()
                    if "html" not in response.headers.get("content-type", "").lower():
                        return None
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        return None
                    length = response.headers.get("content-length", "")
                    if max_bytes and length and int(length) > max_bytes:
                        return None
                    body = bytearray()
                    # 多读出最多一个字节的窗口，让“首块已超过上限”在读取
                    # 下一块前就被拒绝，同时允许恰好达到上限的合法正文完成。
                    chunk_size = min(8192, max_bytes + 1) if max_bytes else 8192
                    async for chunk in response.aiter_raw(chunk_size=chunk_size):
                        if max_bytes and len(body) + len(chunk) > max_bytes:
                            return None
                        body.extend(chunk)
                    encoding = response.encoding or "utf-8"
                    text = extract_text(bytes(body).decode(encoding, errors="replace"))
                    if text:
                        return {"url": current, "text": text, "fetched_at": _now_iso(), "via_proxy": proxy_mode}
                    return None
        return None

    try:
        try:
            direct = await asyncio.wait_for(_fetch(), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, OSError, ValueError, TypeError, LookupError, asyncio.TimeoutError) as exc:
            logger.warning("抓页直连失败（%s）", type(exc).__name__)
            direct = None
        if direct or not proxy_url:
            return direct
        logger.info("抓页直连失败，使用回环代理重试: %s", urlparse(proxy_url).netloc)
        return await asyncio.wait_for(_fetch(proxy=proxy_url), timeout=timeout)
    except asyncio.CancelledError:
        raise
    except (httpx.HTTPError, OSError, ValueError, TypeError, LookupError, asyncio.TimeoutError) as exc:
        logger.warning("抓页代理回退失败（%s）", type(exc).__name__)
        return None


def _normalize_extracted_text(value: str, *, max_chars: int) -> str:
    text = html.unescape(value or "")
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n\n", text)
    text = text.strip()
    return text if max_chars <= 0 else text[:max_chars]


def _extract_main_text(markup: str, *, max_chars: int) -> str:
    """用 lxml 选正文主体；解析失败返回空串交给正则回退。"""
    if lxml_html is None:
        return ""
    try:
        document = lxml_html.fromstring(markup or "")
        for node in document.xpath(
            "//script|//style|//nav|//aside|//footer|//header|//form|//iframe|//noscript|"
            "//*[contains(translate(@class, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'advert')]|"
            "//*[contains(translate(@class, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'comment')]"
        ):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
        candidates = document.xpath(
            "//article|//main|//*[@role='main']|//*[@id='content']|"
            "//*[contains(concat(' ', normalize-space(@class), ' '), ' content ')]"
        )
        root = candidates[0] if candidates else document
        text = "\n".join(part.strip() for part in root.itertext() if part.strip())
        return _normalize_extracted_text(text, max_chars=max_chars)
    except (AttributeError, TypeError, ValueError, LookupError):
        return ""


def extract_text(markup: str) -> str:
    """HTML → 有界正文；lxml 优先，解析失败时回退纯正则实现。"""
    max_chars = _bounded_limit(
        getattr(settings, "search_max_text_chars", 12_000), 12_000, 50_000,
    )
    extracted = _extract_main_text(markup, max_chars=max_chars)
    if extracted:
        return extracted
    body = _NOISE_BLOCK_RE.sub(" ", markup or "")
    body = _NOISE_ATTR_RE.sub(" ", body)
    body = _TAG_RE.sub("\n", body)
    return _normalize_extracted_text(body, max_chars=max_chars)


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


def _source_host(item: dict[str, Any]) -> str:
    try:
        return (urlparse(str(item.get("url") or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _source_body(item: dict[str, Any]) -> str:
    for key in ("content", "text", "summary"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def source_quality_signals(
    item: dict[str, Any],
    *,
    query: str = "",
    source_count: int | None = None,
) -> dict[str, Any]:
    """提取跨类别来源质量信号；只做减法门禁，不把域名当官方白名单。"""
    body = _source_body(item)
    title = str(item.get("title") or "").strip()
    source = str(item.get("source") or "").strip()
    host = _source_host(item)
    haystack = " ".join((title, body, source, str(item.get("url") or "")))
    target = _reference_title_text(query) if query else ""
    if not target and query:
        target = clean_query(query)
    entity_hit = 0.0
    if target:
        needle = _novel_compact(target)
        compact = _novel_compact(haystack)
        entity_hit = max(
            1.0 if needle and needle in compact else 0.0,
            _similar(_title_features(target), _title_features(haystack)),
        )
    token_count = max(1, len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", body)))
    boilerplate_hits = len(_QUALITY_BOILERPLATE_RE.findall(body))
    boilerplate_ratio = min(1.0, boilerplate_hits / token_count)
    has_facts = bool(_QUALITY_FACT_RE.search(body))
    try:
        independent_sources = int(item.get("source_count") or source_count or 1)
    except (TypeError, ValueError, OverflowError):
        independent_sources = 1
    independent_sources = max(1, min(independent_sources, 20))
    low_domain = _host_matches(host, _LOW_QUALITY_DOMAINS)
    low_template = bool(_LOW_QUALITY_TEXT_RE.search(haystack))
    # 这是已知低质来源的减法信号，不是按类别建立的一手来源白名单；
    # 其余来源仍只由正文内容信号决定，模板噪声需要达到比例门槛才剔除。
    hard_reject = bool(low_domain or (low_template and boilerplate_ratio >= 0.04))
    body_score = min(1.0, len(body) / 1200.0)
    multi_source_score = min(1.0, independent_sources / 3.0)
    score = (
        body_score * 0.35
        + entity_hit * 0.25
        + (1.0 if has_facts else 0.0) * 0.20
        + (1.0 - boilerplate_ratio) * 0.10
        + multi_source_score * 0.10
    )
    return {
        "body_len": len(body),
        "entity_hit": round(entity_hit, 4),
        "has_facts": has_facts,
        "boilerplate_ratio": round(boilerplate_ratio, 6),
        "source_count": independent_sources,
        "low_domain": low_domain,
        "low_quality": hard_reject,
        "quality_score": round(max(0.0, min(1.0, score)), 4),
    }


def rank_general_results(query: str, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """通用来源质量门禁；低质命中剔除，其余按内容信号稳定排序。"""
    items = [item for item in (results or []) if isinstance(item, dict)]
    host_counts: dict[str, int] = {}
    for item in items:
        host = _source_host(item) or str(item.get("url") or "")
        host_counts[host] = host_counts.get(host, 0) + 1
    ranked: list[tuple[float, int, dict[str, Any]]] = []
    for index, item in enumerate(items):
        host = _source_host(item) or str(item.get("url") or "")
        signals = source_quality_signals(
            item,
            query=query,
            source_count=host_counts.get(host, 1),
        )
        if signals["low_quality"]:
            continue
        enriched = dict(item)
        enriched["_web_quality"] = signals
        ranked.append((float(signals["quality_score"]), index, enriched))
    ranked.sort(key=lambda value: (-value[0], value[1]))
    return [item for _, _, item in ranked]


def compact_source_text(text: str, *, query: str = "", max_chars: int = 1200) -> str:
    """围绕实体、事实数字和字段标签压缩正文，保留原文顺序且有界。"""
    value = html.unescape(str(text or ""))
    value = _WS_RE.sub(" ", value)
    value = _NL_RE.sub("\n\n", value).strip()
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    chunks = [part.strip() for part in re.split(r"\n{2,}|(?<=[。！？!?])\s*", value) if part.strip()]
    if not chunks:
        return value[:max_chars]
    target = _reference_title_text(query) if query else ""
    if not target and query:
        target = clean_query(query)
    target_compact = _novel_compact(target)
    target_features = _title_features(target)
    scored: list[tuple[float, int, str]] = []
    for index, chunk in enumerate(chunks):
        compact = _novel_compact(chunk)
        score = 0.0
        if target_compact and target_compact in compact:
            score += 4.0
        score += _similar(target_features, _title_features(chunk)) * 3.0
        if _QUALITY_FACT_RE.search(chunk):
            score += 2.0
        if re.search(r"作者|主角|类型|分类|时间|日期|章节|字数|版本|发布|更新|简介|设定", chunk, re.IGNORECASE):
            score += 1.5
        if index == 0:
            score += 0.5
        score -= min(1.0, len(_QUALITY_BOILERPLATE_RE.findall(chunk)) / 3.0)
        scored.append((score, index, chunk))
    selected: list[tuple[int, str]] = []
    used = 0
    for _, index, chunk in sorted(scored, key=lambda value: (-value[0], value[1])):
        if used >= max_chars:
            break
        remaining = max_chars - used
        piece = chunk if len(chunk) <= remaining else chunk[:remaining].rstrip()
        if not piece:
            continue
        selected.append((index, piece))
        used += len(piece)
    selected.sort(key=lambda value: value[0])
    return " ".join(piece for _, piece in selected).strip()[:max_chars]


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


def source_links_requested(message: str) -> bool:
    """只把明确索要出处视为展示链接请求；否定表达不算。"""
    text = str(message or "")[:800]
    text = re.sub(r"(?:《[^》]*》|「[^」]*」|https?://\S+)", "", text)
    if re.search(r"(?:不要|不用|别|无需|不必).{0,8}(?:来源|链接|出处|资料页)", text):
        return False
    return bool(re.search(
        r"(?:给|发|贴|附|看|要|提供|列出|查看|找).{0,8}(?:来源|链接|出处|资料页)"
        r"|(?:来源|链接|出处|资料页).{0,8}(?:在哪|哪里|是什么|呢|给|发|贴|提供|看看)"
        r"|^(?:来源|链接|出处|资料页)[？?！!。\s]*$", text,
    ))


def is_novel_research(plan: dict[str, Any]) -> bool:
    return (plan.get("web_research_kind") or plan.get("research_kind")) == "novel"


def without_source_links(text: str) -> str:
    """移除小说前台引用尾注和 URL，保留正文；后台证据不变。"""
    value = re.split(r"(?:^|\n)\s*(?:参考来源|参考资料|来源链接|资料来源)\s*[:：]", text, maxsplit=1)[0]
    value = re.sub(r"\[([^\]\n]+)\]\(https?://[^\s)]+\)", r"\1", value, flags=re.I)
    value = re.sub(r"(?:https?://|www\.)[^\s<>，。；！？、）)\]】]+", "", value, flags=re.I)
    value = re.sub(r"[（(]\s*[)）]", "", value)
    return value.strip()


def novel_excerpt_reply(sources: list[dict[str, Any]]) -> str:
    """生成前台可见的结构化资料兜底，不展示内部卡片或质量字段。"""
    safe_sources = [
        item for item in (sources or [])[:6]
        if isinstance(item, dict) and _is_safe_url(str(item.get("url") or ""))
    ]
    fields = novel_evidence_fields(safe_sources)
    labels = (
        ("author", "作者"), ("genres", "类型"), ("protagonist", "主角"),
        ("status", "连载状态"), ("started", "连载开始"), ("updated", "更新时间"),
        ("chapter", "最新章节"), ("word_count", "字数"),
    )
    lines = [f"{label}：{fields[key]}" for key, label in labels if fields.get(key)]
    if fields.get("synopsis"):
        lines.append(f"简介：{fields['synopsis']}")
    if fields.get("clues"):
        lines.append(f"设定线索：{fields['clues']}")
    for item in safe_sources:
        text = str(item.get("text") or item.get("summary") or "").strip()
        text = " ".join(without_source_links(text).split())
        # 摘要可能是搜索片段而不是完整句；保留有界片段，不因缺句号丢掉证据。
        sentences = re.findall(r"[^。！？\n]+[。！？]", text)
        excerpt = ""
        if sentences:
            for sentence in sentences:
                if len(excerpt) + len(sentence) > 360:
                    break
                excerpt += sentence
        elif text:
            excerpt = text[:360].rstrip("，,；; ") + "。"
        if excerpt and not fields.get("synopsis"):
            lines.append(f"资料摘录：{excerpt}")
            break
    if lines:
        lines.append("以上只依据本轮公开资料；未覆盖的字段暂时无法核实。")
        return "\n".join(lines)
    return "查到了作品资料，但现有摘录不足以概括内容或评价整本书。"


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
        url = str(item.get("url") or "").strip()
        published = str(item.get("published_at") or "").strip()
        if not title or not source or not _is_safe_url(url):
            continue
        time_known = item.get("time_known", True)
        if not published:
            stamp = "时间未见标注"
        elif time_known:
            stamp = published
        else:
            stamp = f"{published}（据链接推断）"
        raw_body = str(
            item.get("summary") or item.get("text") or item.get("content") or ""
        ).strip()
        summary = compact_source_text(raw_body, query=title, max_chars=200)
        line = f"- {title}（{source}，{stamp}）"
        line += f"\n  来源链接：{url}"
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
    category: str = "news",
    deep_dive: bool = False,
    max_attempts: int | None = None,
    budget_seconds: float | None = None,
    engines: str | None = None,
) -> dict[str, Any]:
    """检索并按事件聚合；命中不足时逐级放宽。

    默认新闻检索三级放宽（按累计URL去重数量达标即停，不把条数当关键事实完整性）：
      1. 清洗后的检索词 + news 类 + 配置时间窗
      2. 同上但去掉时间窗   ← 事件报道常年躺在索引里，硬限"最近一周"会整批排除
      3. 通用网页类 + 无时间窗 ← 覆盖非新闻站的报道
    ``category="general"`` 时首轮即使用通用网页且不带时间窗，适合书名/作品资料查询。
    另可传 ``alt_query``（如 planner 给出的规范化查询）作为备用检索词。
    ``max_attempts`` 限制本函数发起的检索调用总数（含阶梯、角度和 GDELT）；
    调查器集成可设 1，避免在调查预算开始前重复放宽。默认保持现有策略。
    ``budget_seconds`` 可收紧整次检索的墙钟预算，耗尽仍返回先前完成的来源。

    达标线用 ``search_min_results``：旧逻辑只在**零结果**时兜底，
    实测整句问法只回 2 条、2 > 0 因而从不放宽，用户看到"只有一条转载稿"，
    而同一事件实际有 52 条。
    """
    search_category = category if category in {"news", "general"} else "news"
    # 所有类别统一剥离口语包装；clean_query 会在结果过短时回退原文，
    # 因此书名号、实体词和真正的检索内容仍会保留。
    cleaned = clean_query(query)[:400]
    alt = clean_query(alt_query)[:400] if alt_query else ""
    candidates = [cleaned] + ([alt] if alt and alt != cleaned else [])
    initial_window = time_range if search_category == "news" else ""

    # 阶梯式放宽：命中不足就逐级松绑，而不是只在"零结果"时兜底一次。
    # 实测教训：整句问法只回 2 条，2 > 0 所以旧逻辑一次都没放宽，
    # 用户看到的是"只有一条转载稿"——而同一事件实际有 52 条。
    attempts: list[tuple[str, str, str]] = []
    for q in candidates:
        attempts.append((q, search_category, initial_window))
    attempts.append((cleaned, search_category, ""))       # 去掉时间窗
    if search_category != "general":
        attempts.append((cleaned, "general", ""))          # 通用网页类
    for q in candidates[1:]:
        attempts.append((q, "general" if search_category != "general" else search_category, ""))

    threshold = _min_results()
    results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    first = (candidates[0], search_category, initial_window)
    used: tuple[str, str, str] | None = None
    last_attempt: tuple[str, str, str] | None = None
    tried: set[tuple[str, str, str]] = set()
    attempt_log: list[dict[str, Any]] = []
    configured_cap = max(1, int(getattr(settings, "search_request_cap", 4)))
    request_cap = int(_bounded_number(max_attempts, configured_cap, 9)) if max_attempts is not None else configured_cap
    # 显式 0 的语义就是零次请求；默认策略仍至少允许一次。
    if max_attempts is not None:
        request_cap = max(0, request_cap)
    # 深挖角度是独立的补充证据，保留至少两条并发槽位；不改变普通查询的预算上限。
    if deep_dive and max_attempts is None:
        request_cap = max(request_cap, 6)
    request_count = 0
    budget_exhausted = False
    deadline = None
    if budget_seconds is not None:
        try:
            budget = float(budget_seconds)
        except (ValueError, TypeError, OverflowError):
            budget = 0.0
        budget = max(0.0, min(budget, 40.0)) if math.isfinite(budget) else 0.0
        deadline = asyncio.get_running_loop().time() + budget

    def remaining() -> float | None:
        return max(0.0, deadline - asyncio.get_running_loop().time()) if deadline is not None else None

    def may_start() -> bool:
        nonlocal budget_exhausted
        left = remaining()
        if left is not None and left <= 0:
            budget_exhausted = True
            return False
        return request_count < request_cap

    raw_hits = 0
    backend_statuses: list[str] = []
    unresponsive_engines: set[str] = set()

    def merge_batch(items: Any) -> int:
        nonlocal raw_hits
        added = 0
        if isinstance(items, SearchBatch):
            raw_hits += items.raw_hits or len(items)
            backend_statuses.append(items.backend_status)
            unresponsive_engines.update(items.unresponsive_engines)
        elif isinstance(items, list):
            raw_hits += len(items)
        for item in items[:100] if isinstance(items, list) else []:
            if not isinstance(item, dict) or len(results) >= 100:
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url or url in seen_urls:
                continue
            seen_urls.add(url)
            results.append(item)
            added += 1
        return added

    async def timed(operation):
        left = remaining()
        return await asyncio.wait_for(operation, timeout=left) if left is not None else await operation

    for q, category, window in attempts:
        if not may_start():
            break
        key = (q, category, window)
        if not q or key in tried:
            continue
        tried.add(key)
        last_attempt = key
        request_count += 1
        record = {"query": q, "category": category, "time_range": window, "received": 0, "added": 0, "status": "completed"}
        attempt_log.append(record)
        try:
            search_kwargs = {
                "category": category,
                "time_range": window,
                "limit": limit,
            }
            if engines:
                search_kwargs["engines"] = engines
            batch = await timed(web_search(q, **search_kwargs))
        except Exception as exc:  # 子次失败/超时不能覆盖已获得的证据；取消仍向上传播
            record["status"] = type(exc).__name__
            if deadline is not None and remaining() == 0:
                budget_exhausted = True
                break
            continue
        record["received"] = len(batch) if isinstance(batch, list) else 0
        record["backend_status"] = getattr(batch, "backend_status", "ok")
        record["added"] = merge_batch(batch)
        if record["added"]:
            used = key  # 最后实际贡献来源的查询，而不是空批次的查询
        if len(results) >= threshold:
            break

    # 追加来源同样即时合并。另一个角度超时，不影响已完成的角度。
    angle_added = 0
    if deep_dive and cleaned and may_start():
        angles = build_angle_queries(cleaned)[:max(0, request_cap - request_count)]

        async def angle(q: str) -> None:
            nonlocal request_count, angle_added, budget_exhausted
            if not may_start():
                return
            request_count += 1
            try:
                search_kwargs = {
                    "category": "general",
                    "time_range": "",
                    "limit": limit,
                }
                if engines:
                    search_kwargs["engines"] = engines
                angle_added += merge_batch(await timed(web_search(q, **search_kwargs)))
            except Exception:
                if deadline is not None and remaining() == 0:
                    budget_exhausted = True

        tasks = [asyncio.create_task(angle(q)) for q in angles]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    gdelt_count = 0
    try:
        from app.chat import gdelt_provider

        if may_start() and gdelt_provider.configured():
            request_count += 1
            gdelt_count = merge_batch(await timed(gdelt_provider.search(cleaned, time_range=time_range, limit=limit)))
    except Exception as exc:  # noqa: BLE001 - 第二源绝不能拖垮已得来源
        if deadline is not None and remaining() == 0:
            budget_exhausted = True
        logger.warning("GDELT 合并失败（%s），仅用已有检索结果", type(exc).__name__)

    # 搜索结果进入任何最终资料块前都经过通用内容门禁。这里不按域名列
    # 白名单，而是用正文长度、实体重叠、事实密度和模板噪声排序/减法。
    before_quality = len(results)
    results = rank_general_results(cleaned, results)
    quality_filtered = max(0, before_quality - len(results))
    events = cluster_events(results) if results else []
    return {
        "query": query,
        "query_used": (used or last_attempt or first)[0],
        "has_sources": bool(results),
        "result_count": len(results),
        # 分开记录两种降级，混在一起无法判断该调哪一处：
        # query_cleaned = 检索词被剥过口语包装；fallback_used = 放宽过类别/时间窗
        "query_cleaned": cleaned != (query or "").strip(),
        "fallback_used": any(key != first for key in tried),
        "attempts": len(tried),
        "attempt_log": attempt_log,
        "request_count": request_count,
        "stage_counts": {
            "raw_hits": raw_hits,
            "kept_after_dedupe": before_quality,
            "kept_after_quality": len(results),
            "quality_filtered": quality_filtered,
            "requests": request_count,
        },
        "backend_status": (
            "cooling_down" if "cooling_down" in backend_statuses else
            "empty_backend" if backend_statuses and all(status == "empty_backend" for status in backend_statuses) else
            "error" if "error" in backend_statuses else
            "timeout" if "timeout" in backend_statuses else
            "ok" if results else (backend_statuses[-1] if backend_statuses else "unavailable")
        ),
        "backend_statuses": list(dict.fromkeys(backend_statuses)),
        "unresponsive_engines": sorted(unresponsive_engines)[:20],
        "budget_exhausted": budget_exhausted,
        "threshold_met": len(results) >= threshold,
        "stop_reason": "budget_exhausted" if budget_exhausted else (
            "quality_gate_empty" if before_quality and not results else
            "threshold_met" if len(results) >= threshold else
            "attempt_limit" if request_count >= request_cap else "search_complete"
        ),
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
