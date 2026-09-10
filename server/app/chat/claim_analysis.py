"""声明级比对：把多篇报道拆成结构化「声明」，按细分维度找冲突。

## 与来源独立性核查的关系

- 来源独立性核查（source_analysis）：回答"有几个独立信源"
- 声明级比对（本模块）：回答"这些信源在具体事实上说的一致吗"

前者防"多来源其实是一个信源"，后者防"把某方主张/自媒体定性当成事实"、
并把"有分歧"精确到"籍贯/调解次数/定性"这些具体点上。

## 三层维度（本期范围）

- 硬事实层（全细分）：时间/数字/地点/身份——同一客观事实不该有两个值，
  冲突=有人错，直接标警惕
- 处置层（带时间戳）：调解/法律/官方/舆论进展——冲突可能只是报道时间不同，
  必须带 reported_at 才能区分"真冲突"和"进展"
- 定性层（只标归属）：性质/因果/责任——冲突是常态，重点是标清"谁说的"；
  本期不做"强度序/升级链"，留待抽取质量验证后再加

## 每条声明的强制标签

- attribution：事实陈述 / 某方主张 / 第三方定性——防止主张被当事实
- single：是否只有一家提——孤证是最容易出错的信息

## 判定纪律

不裁决"谁对"。没有官方通报时只标冲突、标归属，不替官方下事实结论。
这与"有立场但不被噪音带走"一致：核心是非可以表态，事实真伪交给官方。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 归属类型：决定这条声明能不能当"事实"用
ATTR_FACT = "事实陈述"
ATTR_PARTY = "某方主张"
ATTR_THIRD = "第三方定性"

# 维度层
LAYER_HARD = "硬事实"
LAYER_DISPOSAL = "处置"
LAYER_JUDGMENT = "定性"

@dataclass
class Claim:
    """一条声明：谁在某个事实维度上声称了什么。"""

    layer: str                       # LAYER_HARD / LAYER_DISPOSAL / LAYER_JUDGMENT
    dimension: str                   # 维度名，如 "时间" "身份"
    sub: str                         # 子维度，如 "事发" "籍贯"
    value: str                       # 声明的值，如 "8月26日" "福建"
    attribution: str = ATTR_FACT     # 归属：事实/某方主张/第三方定性
    sources: list[str] = field(default_factory=list)   # 提到这条的信源/域名
    reported_at: str = ""            # 报道时间（处置层强制）

    @property
    def key(self) -> str:
        """对齐口径：同 layer+dimension+sub 的声明放一起比。"""
        return f"{self.layer}/{self.dimension}/{self.sub}"

    @property
    def single(self) -> bool:
        """孤证：只有一个信源提到。"""
        return len(self.sources) <= 1


@dataclass
class Conflict:
    """同一子维度下出现互不相同的值。"""

    key: str
    layer: str
    dimension: str
    sub: str
    variants: list[Claim]            # 冲突的各个版本
    kind: str = "冲突"               # 冲突 / 进展 / 各执一词

    @property
    def is_progress(self) -> bool:
        return self.kind == "进展"


@dataclass
class ClaimSet:
    claims: list[Claim] = field(default_factory=list)
    extracted_by_llm: bool = False

    def by_key(self) -> dict[str, list[Claim]]:
        groups: dict[str, list[Claim]] = {}
        for c in self.claims:
            groups.setdefault(c.key, []).append(c)
        return groups

    @property
    def singles(self) -> list[Claim]:
        return [c for c in self.claims if c.single]


# ── 规则抽取：零 LLM，抽硬事实层结构化维度 ──────────────────

# 日期：2026年8月26日 / 8月26日 / 8月26日中午
_DATE_RE = re.compile(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日")
# 报道时间标记词：出现这些说明句中日期是"报道/发布"时间而非事发
_REPORT_TIME_HINT = ("报道", "发布", "刊发", "评论", "获悉", "消息", "讯")
# 次数
_COUNT_RE = re.compile(r"(?<![0-9])(一|二|两|三|四|五|六|七|八|九|十|\d+)\s*次")
# 年龄
_AGE_RE = re.compile(r"(\d{1,3})\s*岁")
# 籍贯/属地：福建女子 / 长沙一名 / 湖南长沙
_ORIGIN_RE = re.compile(
    r"(北京|上海|天津|重庆|河北|山西|辽宁|吉林|黑龙江|江苏|浙江|安徽|福建|"
    r"江西|山东|河南|湖北|湖南|广东|广西|海南|四川|贵州|云南|陕西|甘肃|青海|"
    r"台湾|内蒙古|宁夏|新疆|西藏)([\u4e00-\u9fa5]{0,3}?)?(?:籍|人|女子|男子|女孩|男孩)"
)

_ZH_NUM = {"一": "1", "二": "2", "两": "2", "三": "3", "四": "4", "五": "5",
           "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}


def _norm_count(raw: str) -> str:
    return _ZH_NUM.get(raw, raw)


def extract_claims_rule(results: list[dict[str, Any]]) -> ClaimSet:
    """规则抽取硬事实层。覆盖不全但零成本、稳定，作为 LLM 抽取的兜底/补充。"""
    cs = ClaimSet()
    for item in results or []:
        domain = str(item.get("source") or "")
        title = str(item.get("title") or "")
        summary = str(item.get("summary") or "")
        text = f"{title} {summary}"

        for m in _DATE_RE.finditer(text):
            # 报道/发布提示词几乎总紧跟在日期之后（"9月8日报道"），
            # 所以只看日期**后面**一小段；往前看会误收上一个日期的提示词。
            span_ctx = text[m.end(): m.end() + 4]
            sub = "报道" if any(h in span_ctx for h in _REPORT_TIME_HINT) else "事发"
            value = f"{m.group(2)}月{m.group(3)}日"
            cs.claims.append(Claim(LAYER_HARD, "时间", sub, value, sources=[domain]))

        # 年龄不做规则抽取：一篇里常有多个主体（4岁男童+19岁女子），
        # 规则分不清谁是谁，直接抽会把两个人的年龄误判成冲突。
        # 这类多主体维度留给 LLM（它能绑定主体）。

        for m in _COUNT_RE.finditer(text):
            ctx = text[max(0, m.start() - 4): m.end() + 4]
            sub = "调解" if "调解" in ctx else "次数"
            cs.claims.append(
                Claim(LAYER_HARD, "数字", sub, f"{_norm_count(m.group(1))}次", sources=[domain])
            )

        seen_origin = set()
        for m in _ORIGIN_RE.finditer(text):
            prov = m.group(1)
            if prov in seen_origin:
                continue
            seen_origin.add(prov)
            cs.claims.append(
                Claim(LAYER_HARD, "身份", "籍贯", prov, sources=[domain])
            )
    return _dedup_merge_sources(cs)


def _dedup_merge_sources(cs: ClaimSet) -> ClaimSet:
    """同一 (key,value) 的声明合并信源，避免一篇里重复计数。"""
    merged: dict[tuple[str, str], Claim] = {}
    for c in cs.claims:
        k = (c.key, c.value)
        if k in merged:
            for s in c.sources:
                if s and s not in merged[k].sources:
                    merged[k].sources.append(s)
        else:
            c.sources = [s for s in dict.fromkeys(c.sources) if s]
            merged[k] = c
    return ClaimSet(claims=list(merged.values()), extracted_by_llm=cs.extracted_by_llm)


# ── LLM 抽取：只抽取不评价，输出结构化声明表 ────────────────

_EXTRACT_INSTRUCTION = """你是事实抽取器，只抽取不评价、不判断谁对。
从下列报道里抽取「声明」，每条声明标注它属于哪个维度、谁说的、来自哪家。

维度分三层：
- 硬事实：时间(事发/报道)、数字(年龄/次数/金额)、地点、身份(籍贯/角色)
- 处置：调解结果、法律进展、官方处置、舆论处置（务必带该报道的时间）
- 定性：性质认定、因果、责任（这类只需标清是谁的判断）

归属(attribution)三选一：
- 事实陈述：客观发生的事
- 某方主张：某个当事人的说法（如"女子称…"）
- 第三方定性：媒体评论/自媒体给的定性（如"猥亵"）

只输出 JSON，格式：
{"claims":[{"layer":"硬事实|处置|定性","dimension":"","sub":"","value":"","attribution":"事实陈述|某方主张|第三方定性","source":"来源域名或媒体","reported_at":""}]}
不要输出任何解释文字。"""


def build_extraction_prompt(results: list[dict[str, Any]], limit: int = 6) -> str:
    """拼装供 LLM 抽取的输入。

    控制规模：轻量模型面对长输入+长输出容易截断或返回空。限 6 条、摘要截到
    120 字，既够比对（同一事件的报道高度重复），又让输出短而稳。
    """
    lines = [_EXTRACT_INSTRUCTION, "", "报道列表："]
    for i, item in enumerate(results[:limit], 1):
        src = str(item.get("source") or "未知来源")
        title = str(item.get("title") or "").strip()
        summary = str(item.get("summary") or "").strip()[:120]
        lines.append(f"[{i}] 来源={src}｜标题：{title}｜摘要：{summary}")
    return "\n".join(lines)


_LAYERS = {LAYER_HARD, LAYER_DISPOSAL, LAYER_JUDGMENT}
_ATTRS = {ATTR_FACT, ATTR_PARTY, ATTR_THIRD}


def _extract_json_block(text: str) -> str:
    """从 LLM 输出里抠出 JSON：容忍 ```围栏、思考前缀、前后夹带文字。"""
    if not text:
        return ""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start: end + 1]
    return ""


def _salvage_claim_objects(text: str) -> list[dict[str, Any]]:
    """截断救援：JSON 整体不完整时，逐个抠出**已闭合**的 claim 对象。

    轻量模型常把声明表写到一半就撞上 token 上限，末尾对象残缺、整个 JSON
    无法解析。但前面若干条是完整的——按大括号配对逐个提取，能救回大部分。
    """
    import json

    objects: list[dict[str, Any]] = []
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    fragment = text[start: i + 1]
                    start = -1
                    try:
                        obj = json.loads(fragment)
                    except (ValueError, TypeError):
                        continue
                    # 顶层对象可能是 {"claims":[...]}（完整前半段），展开它
                    if isinstance(obj, dict) and isinstance(obj.get("claims"), list):
                        for c in obj["claims"]:
                            if isinstance(c, dict) and (c.get("value") or c.get("layer")):
                                objects.append(c)
                    elif isinstance(obj, dict) and (obj.get("value") or obj.get("layer")):
                        objects.append(obj)
    # 若整体只有一个未闭合的外层 {"claims":[ ...，逐个抠内部已闭合对象
    if not objects:
        for m in re.finditer(r"\{[^{}]*\}", text):
            try:
                obj = json.loads(m.group())
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict) and (obj.get("value") or obj.get("layer")):
                objects.append(obj)
    return objects


def parse_claims(json_text: str) -> ClaimSet:
    """解析 LLM 抽取结果；任何异常都返回空集，绝不抛出。"""
    import json

    block = _extract_json_block(json_text or "")
    raw: Any = None
    if block:
        try:
            data = json.loads(block)
            raw = data.get("claims") if isinstance(data, dict) else None
        except (ValueError, TypeError):
            raw = None
    # 整体解析失败或不含 claims 列表 → 截断救援：捞已闭合的 claim 对象
    if not isinstance(raw, list):
        raw = _salvage_claim_objects(json_text or "")
    if not raw:
        return ClaimSet()

    cs = ClaimSet(extracted_by_llm=True)
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        layer = str(entry.get("layer") or "").strip()
        value = str(entry.get("value") or "").strip()
        if layer not in _LAYERS or not value:
            continue
        attribution = str(entry.get("attribution") or ATTR_FACT).strip()
        if attribution not in _ATTRS:
            attribution = ATTR_FACT
        src = str(entry.get("source") or "").strip()
        cs.claims.append(Claim(
            layer=layer,
            dimension=str(entry.get("dimension") or "").strip() or "其他",
            sub=str(entry.get("sub") or "").strip() or "其他",
            value=value,
            attribution=attribution,
            sources=[src] if src else [],
            reported_at=str(entry.get("reported_at") or "").strip(),
        ))
    return _dedup_merge_sources(cs)


# ── 合并规则与 LLM 结果 ─────────────────────────────────────

def merge(rule_cs: ClaimSet, llm_cs: ClaimSet) -> ClaimSet:
    """规则与 LLM 抽取按 (key,value) 合并去重，信源取并集。

    LLM 通常更全（能抽定性/处置），规则更稳（时间/数字/籍贯）；
    合并后互补。归属以 LLM 为准（它能判"某方主张"），规则条目默认事实陈述。
    """
    combined = ClaimSet(
        claims=list(llm_cs.claims) + list(rule_cs.claims),
        extracted_by_llm=llm_cs.extracted_by_llm,
    )
    return _dedup_merge_sources(combined)


# ── 冲突检测 ────────────────────────────────────────────────

def _values_differ(a: str, b: str) -> bool:
    """值是否实质不同：一个是另一个子串时视为同一说法的粗细差异，不算冲突。"""
    a, b = a.strip(), b.strip()
    if a == b:
        return False
    return a not in b and b not in a


def _later(a: str, b: str) -> bool:
    """粗判 a 的报道时间是否晚于 b（同格式 M月D日）。无法判定返回 False。"""
    ma = _DATE_RE.search(a or "")
    mb = _DATE_RE.search(b or "")
    if not ma or not mb:
        return False
    return (int(ma.group(2)), int(ma.group(3))) > (int(mb.group(2)), int(mb.group(3)))


def detect_conflicts(cs: ClaimSet) -> list[Conflict]:
    """按子维度分组，组内多个不同值即冲突；按层判定冲突性质。"""
    conflicts: list[Conflict] = []
    for key, group in cs.by_key().items():
        # 组内去重到"实质不同的值"
        uniq: list[Claim] = []
        for c in group:
            if all(_values_differ(c.value, v.value) for v in uniq):
                uniq.append(c)
        if len(uniq) < 2:
            continue

        layer = uniq[0].layer
        if layer == LAYER_JUDGMENT:
            kind = "各执一词"
        elif layer == LAYER_DISPOSAL:
            # 处置层：若两个版本报道时间有先后，多半是进展而非冲突
            times = [c.reported_at for c in uniq if c.reported_at]
            kind = "进展" if len(times) >= 2 and (
                _later(times[0], times[1]) or _later(times[1], times[0])
            ) else "冲突"
        else:
            kind = "冲突"
        conflicts.append(Conflict(
            key=key, layer=layer, dimension=uniq[0].dimension,
            sub=uniq[0].sub, variants=uniq, kind=kind,
        ))
    return conflicts


# ── 渲染：产出注入 prompt 的比对结论 ────────────────────────

def _fmt_variant(c: Claim) -> str:
    who = "" if c.attribution == ATTR_FACT else f"（{c.attribution}）"
    src = f"[{c.sources[0]}]" if c.sources else ""
    when = f"·{c.reported_at}" if c.reported_at else ""
    return f"{c.value}{who}{src}{when}"


def render(cs: ClaimSet, conflicts: list[Conflict]) -> str:
    """比对结论文本；没有可说的返回空串。"""
    lines: list[str] = []

    hard = [c for c in conflicts if c.layer == LAYER_HARD and not c.is_progress]
    judgment = [c for c in conflicts if c.layer == LAYER_JUDGMENT]
    disposal = [c for c in conflicts if c.layer == LAYER_DISPOSAL]
    progress = [c for c in disposal if c.is_progress]
    disposal_conflict = [c for c in disposal if not c.is_progress]

    if hard:
        lines.append("硬事实对不上的地方（同一客观事实出现不同值，需警惕有人搞错）：")
        for c in hard:
            lines.append(f"  · {c.dimension}-{c.sub}：" + " / ".join(_fmt_variant(v) for v in c.variants))

    if disposal_conflict:
        lines.append("进展/处置说法不一致：")
        for c in disposal_conflict:
            lines.append(f"  · {c.dimension}-{c.sub}：" + " / ".join(_fmt_variant(v) for v in c.variants))

    if progress:
        lines.append("以下是进展而非矛盾（报道时间不同，属事件推进，不要当冲突）：")
        for c in progress:
            lines.append(f"  · {c.dimension}-{c.sub}：" + " → ".join(_fmt_variant(v) for v in c.variants))

    if judgment:
        lines.append("定性上各执一词（属正常分歧，务必说清是谁的判断，不要替官方裁决）：")
        for c in judgment:
            lines.append(f"  · {c.dimension}-{c.sub}：" + " / ".join(_fmt_variant(v) for v in c.variants))

    # 孤证与第三方定性提醒
    singles = [c for c in cs.singles if c.attribution != ATTR_FACT]
    third = [c for c in cs.claims if c.attribution == ATTR_THIRD]
    if third:
        vals = "、".join(dict.fromkeys(f"{c.value}" for c in third))
        lines.append(f"注意：「{vals}」是第三方/自媒体给的定性，不是报道事实，不能采信为事实。")
    if singles:
        vals = "、".join(dict.fromkeys(f"{c.dimension}-{c.sub}={c.value}" for c in singles[:4]))
        lines.append(f"注意：以下为孤证（仅一家提到），不足以作为事实依据：{vals}。")

    return "\n".join(lines)


async def extract_claims_llm(results: list[dict[str, Any]], runtime: Any) -> ClaimSet:
    """调用一次 LLM 抽取声明表；任何失败都返回空集（调用方降级到规则结果）。"""
    import asyncio

    settings = runtime.settings
    if not results:
        return ClaimSet()
    budget = max(1.0, float(getattr(settings, "claim_analysis_budget", 12.0)))
    model = str(getattr(settings, "reflection_review_model", "") or "").strip() or settings.llm_model

    prompt = build_extraction_prompt(results)
    max_tokens = max(200, int(getattr(settings, "claim_analysis_max_tokens", 1200)))
    attempts = max(1, int(getattr(settings, "claim_analysis_retries", 2)) + 1)

    async def _call() -> str:
        return await runtime.llm.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            timeout=budget,
            model=model,
            purpose="claim_extract",
            retry_budget=0,
        )

    # 实测这个轻量模型对结构化抽取返回不稳定（同一输入时空时不空），
    # 故在总预算内重试若干次，任一非空即用；仍失败就降级到规则层。
    deadline = asyncio.get_event_loop().time() + budget
    for _ in range(attempts):
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0.5:
            break
        try:
            text = await asyncio.wait_for(_call(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        except Exception as exc:  # noqa: BLE001
            runtime.logger.warning("声明抽取调用失败: %s", type(exc).__name__)
            continue
        cs = parse_claims(text)
        if cs.claims:
            return cs
    runtime.logger.warning("声明抽取多次未取得结果，降级到规则层")
    return ClaimSet()


@dataclass
class ClaimReport:
    text: str = ""
    conflict_count: int = 0
    single_count: int = 0
    extracted_by_llm: bool = False


def analyze_claims(rule_cs: ClaimSet, llm_cs: ClaimSet | None = None) -> ClaimReport:
    """编排：合并 → 检测冲突 → 渲染。llm_cs 为空则只用规则结果。"""
    cs = merge(rule_cs, llm_cs) if llm_cs else rule_cs
    conflicts = detect_conflicts(cs)
    text = render(cs, conflicts)
    return ClaimReport(
        text=text,
        conflict_count=len([c for c in conflicts if not c.is_progress]),
        single_count=len(cs.singles),
        extracted_by_llm=cs.extracted_by_llm,
    )


__all__ = [
    "Claim",
    "Conflict",
    "ClaimSet",
    "ClaimReport",
    "extract_claims_rule",
    "build_extraction_prompt",
    "extract_claims_llm",
    "parse_claims",
    "merge",
    "detect_conflicts",
    "render",
    "analyze_claims",
]
