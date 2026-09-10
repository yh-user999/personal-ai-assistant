"""来源独立性核查：区分「载体」「原始信源」「发布者」。

## 为什么需要

检索结果里的 ``source`` 字段是**平台域名**（新浪、腾讯、搜狐）。同一篇稿子被
多家平台转载时，域名各不相同，于是"N 条报道"会被当成"N 方印证"——而实际
原创方可能只有一家。

实测（「湖南四岁幼童事件」8 条结果）：4 条是同一篇人民日报评论的转载
（新浪 2 条、凤凰 1 条、腾讯 1 条），独立原创方只有 3~4 个。

真实性判断依赖的是**原始信源数**，不是域名数。本模块把它算出来，
让回复能诚实地说"这其实是一家官媒的立场被转了四次"。

## 三层概念

- 载体（平台/域名）：新浪、腾讯、搜狐——转载渠道，换一家不增加独立性
- 原始信源（原创方）：人民日报、法制日报、腾讯记者——这才是独立性依据
- 发布者（用户）：微博账号、X 用户——社交平台才需要，新闻站没有这一层

## 判定策略

信号 A（出处标注，权威）→ 信号 B（文本重合，兜底）→ 都不中则标为「无法判定」。
**无法判定时不默认算独立**——那等于又虚高一次；宁可少算。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ── 已知媒体名（用于把简称/栏目名归一到一个信源）──────────
_ALIASES: dict[str, str] = {
    "人民锐评": "人民日报",
    "人民日报客户端": "人民日报",
    "人民网": "人民日报",
    "新华社客户端": "新华社",
    "央视": "央视新闻",
    "央视网": "央视新闻",
    "中新社": "中国新闻社",
    "中新网": "中国新闻社",
    "中新经纬": "中国新闻社",
    "央视新闻客户端": "央视新闻",
}

# 常见原创媒体；命中即直接认定为原始信源
_KNOWN_OUTLETS: tuple[str, ...] = (
    "人民日报", "新华社", "央视新闻", "光明日报", "经济日报", "环球时报",
    "中国青年报", "法治日报", "法制日报", "检察日报", "中国新闻社",
    "澎湃新闻", "新京报", "南方都市报", "南方周末", "三联生活周刊",
    "财新", "第一财经", "界面新闻", "封面新闻", "红星新闻", "极目新闻",
    "上游新闻", "九派新闻", "中国新闻周刊", "每日经济新闻", "财联社",
    "证券时报", "上海证券报", "中国证券报", "时代周报", "观察者网",
    "北京日报", "广州日报", "成都商报", "潇湘晨报", "大皖新闻",
    "联合早报", "路透社", "彭博", "美联社", "法新社", "BBC", "纽约时报",
    "金融时报", "华尔街日报", "南华早报",
)

# 出处句式：按可靠性从高到低尝试
_ORIGIN_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 据《法制日报》《封面新闻》等媒体报道
    re.compile(r"据\s*《([^》]{2,20})》"),
    re.compile(r"据\s*([\u4e00-\u9fa5A-Za-z]{2,12})\s*(?:等媒体)?(?:报道|消息|称|披露)"),
    # 人民日报客户端发表评论文章 / 中新社发布
    re.compile(r"([\u4e00-\u9fa5]{2,12}(?:日报|晚报|时报|周刊|新闻|网|社|电视台))\s*(?:客户端)?\s*(?:发表|发布|刊发|消息)"),
    re.compile(r"来源[:：]\s*([\u4e00-\u9fa5A-Za-z]{2,20})"),
    # 标题栏署名：人民日报评… / 人民锐评：…
    re.compile(r"^([\u4e00-\u9fa5]{2,12}?)\s*(?:锐评|评论|评)"),
)

# 域名 → 原始信源：**只列本身就是原创媒体的域名**。
# 门户（sina / qq / sohu / ifeng / 163）刻意不在表内——它们只是载体，
# 把载体当信源会让"被转载"看起来像"原创"。
_DOMAIN_ORIGINS: tuple[tuple[str, str], ...] = (
    ("people.com.cn", "人民日报"),
    ("news.cn", "新华社"),
    ("xinhuanet.com", "新华社"),
    ("chinanews.com", "中国新闻社"),
    ("thepaper.cn", "澎湃新闻"),
    ("bjnews.com.cn", "新京报"),
    ("caixin.com", "财新"),
    ("yicai.com", "第一财经"),
    ("jiemian.com", "界面新闻"),
    ("thecover.cn", "封面新闻"),
    ("zaobao.com.sg", "联合早报"),
    ("cctv.com", "央视新闻"),
    ("gmw.cn", "光明日报"),
    ("legaldaily.com.cn", "法治日报"),
)

# 同一篇稿子的转载：比较开头这段（转载常逐字复制开头）
_SIGNATURE_CHARS = 120
_SIMILARITY_THRESHOLD = 0.72


def normalize_origin(name: str) -> str:
    """媒体名归一：别名 → 规范名；不认识就原样返回（不丢信息）。"""
    value = (name or "").strip().strip("《》\"'“”")
    if not value:
        return ""
    if value in _ALIASES:
        return _ALIASES[value]
    for known in _KNOWN_OUTLETS:
        if known in value or value in known:
            return known
    return value


def extract_origin(text: str) -> str:
    """从标题或摘要里抽取原始信源；抽不到返回空串。"""
    value = (text or "").strip()
    if not value:
        return ""
    for pattern in _ORIGIN_PATTERNS:
        match = pattern.search(value)
        if match:
            candidate = normalize_origin(match.group(1))
            # 太短或纯功能词的多半是误匹配
            if len(candidate) >= 2 and candidate not in {"记者", "报道", "消息", "近日"}:
                return candidate
    # 不设"文中出现媒体名即算信源"的弱信号：**提及 ≠ 原创**。
    # 实测踩坑：腾讯自采稿因标题带"人民日报发声"、联合早报稿因引述人民日报
    # 评论，双双被归到人民日报名下，把 4 条转载误算成 6 条，还抹掉了两个
    # 真正独立的信源——核查本身反而制造了虚假的集中。
    return ""


def origin_from_domain(domain: str) -> str:
    """按域名判断原始信源。

    只映射**本身就是原创媒体**的域名（人民网、联合早报、澎湃…）；
    门户（新浪/腾讯/搜狐/凤凰/网易）是转载载体，绝不能映射成信源，
    否则会把"被转载"当成"原创方"。
    """
    host = (domain or "").strip().lower()
    if not host:
        return ""
    for suffix, origin in _DOMAIN_ORIGINS:
        if host == suffix or host.endswith("." + suffix):
            # 门户类即使命中也不认（它们在表里就不该出现）
            return origin
    return ""


def _signature(item: dict[str, Any]) -> str:
    """取开头文本作为"同稿"指纹（去掉标签与标点，减少排版差异干扰）。"""
    raw = f"{item.get('title', '')} {item.get('summary', '')}"
    text = re.sub(r"[\s\u3000]+", "", raw)
    text = re.sub(r"[，。、；：,.!?！？…\-—（）()\[\]【】\"'“”‘’]", "", text)
    return text[:_SIGNATURE_CHARS]


def _similar(a: str, b: str) -> float:
    """2-gram 交集占较短串的比例（转载逐字复制开头，用包含度比 Jaccard 稳）。"""
    if not a or not b:
        return 0.0
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < 12:
        return 1.0 if short and short in long else 0.0
    grams = {short[i : i + 2] for i in range(len(short) - 1)}
    other = {long[i : i + 2] for i in range(len(long) - 1)}
    if not grams:
        return 0.0
    return len(grams & other) / len(grams)


@dataclass
class SourceAnalysis:
    total: int = 0
    clusters: list[dict[str, Any]] = field(default_factory=list)
    unknown: list[dict[str, Any]] = field(default_factory=list)

    @property
    def confirmed_count(self) -> int:
        """已确认的原创信源数。不把"无法判定"算进来。"""
        return len(self.clusters)

    @property
    def max_reprint(self) -> int:
        return max((c["count"] for c in self.clusters), default=0)

    @property
    def unknown_count(self) -> int:
        total = 0
        for entry in self.unknown:
            total += entry["count"]
        return total

    def render(self) -> str:
        """给 prompt 用的核查结论。

        刻意不折算成单一的"独立信源 N 个"——那会把"无法判定"混进确认数，
        反而制造虚假精度。分开报：确认为哪些、无法判定多少。
        """
        if self.total == 0:
            return ""
        lines = [f"共 {self.total} 条报道，按原始信源归并后："]
        for cluster in sorted(self.clusters, key=lambda c: -c["count"]):
            suffix = "  ← 同一篇的转载" if cluster["count"] > 1 else ""
            lines.append(
                f"  · {cluster['origin']}：{cluster['count']} 条"
                f"（{('、'.join(cluster['domains'][:4]))}）{suffix}"
            )
        if self.unknown:
            hosts = "、".join(u["domains"][0] for u in self.unknown[:4] if u["domains"])
            lines.append(
                f"  · 无法判定原始出处：{self.unknown_count} 条（{hosts}）"
            )
        if self.max_reprint >= 2:
            lines.append(
                f"  注意：有 {self.max_reprint} 条来自同一信源，"
                f"报道条数不等于独立信源数，不要用条数当作多方印证。"
            )
        if self.confirmed_count == 0:
            lines.append("  注意：没有一条能确认原始出处，无法判断是否多方印证。")
        return "\n".join(lines)


def analyze_sources(results: list[dict[str, Any]]) -> SourceAnalysis:
    """按原始信源归并检索结果。纯文本处理，零 LLM、零额外请求。

    判定顺序：出处句式 → 原创媒体域名 → 文本重合（同稿转载）。
    三者都不中即归入"无法判定"，**不默认算独立**。
    """
    analysis = SourceAnalysis(total=len(results or []))
    for item in results or []:
        domain = str(item.get("source") or "")
        text = f"{item.get('title', '')} {item.get('summary', '')}"
        origin = extract_origin(text) or origin_from_domain(domain)
        sig = _signature(item)
        placed = False

        for cluster in analysis.clusters:
            # 同源名称一致 → 直接归并
            if origin and origin == cluster["origin"]:
                same = True
            # 文本高度重合 → 同一篇稿子的转载（含"无法判定"之间的互认）
            else:
                same = _similar(sig, cluster["_sig"]) >= _SIMILARITY_THRESHOLD
            if same:
                cluster["count"] += 1
                if domain and domain not in cluster["domains"]:
                    cluster["domains"].append(domain)
                placed = True
                break

        if placed:
            continue
        if origin:
            analysis.clusters.append({
                "origin": origin, "count": 1,
                "domains": [domain] if domain else [], "_sig": sig,
            })
            continue

        # 无出处：与已有"无法判定"条目比对文本，同稿也只是同一簇
        for entry in analysis.unknown:
            if _similar(sig, entry["_sig"]) >= _SIMILARITY_THRESHOLD:
                entry["count"] += 1
                if domain and domain not in entry["domains"]:
                    entry["domains"].append(domain)
                placed = True
                break
        if not placed:
            analysis.unknown.append({
                "domains": [domain] if domain else [], "count": 1, "_sig": sig,
            })
    return analysis
