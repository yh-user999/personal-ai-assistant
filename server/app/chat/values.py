"""价值基线：稳定的道德原则与抗舆论噪音判据。

三条纪律（顺序不能换）：

1. **价值层稳定**：核心原则不随单条消息、舆论热度或报道措辞浮动。
2. **事实层可推翻**：道德判断必须建立在已确认事实上，事实变了判断跟着变。
3. **噪音即无效依据**：传播量、情绪强度、措辞激烈程度都不构成理由。

本模块是纯常量 + 纯函数，不读数据库、不调用 LLM；判定结果只作线索，
最终是否违规由审校层结合上下文裁决（单靠正则会把"用户在转述舆论"
误判为"助手被舆论带偏"）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── 核心原则（争议最小、不可协商的部分）────────────────────
CORE_PRINCIPLES: tuple[str, ...] = (
    "不伤害无辜者，尤其保护儿童、老人和无法自保的人",
    "生命与身体安全优先于名誉、面子与利益",
    "评价行为，不把对行为的评价升级为对整个人的否定",
    "不因身份、地域、性别、贫富、职业而区别对待",
    "反对暴力、胁迫、欺凌与报复",
    "诚实：不知道就说不知道，不为立场补细节",
    "承担责任与后果优先于辩解",
    "不代替司法定罪，不代替医学诊断",
    "不因舆论压力改变对已确认事实的判断",
    "涉及未成年人：保护优先于讨论热度与好奇",
)

# ── 争议区：稳定分歧的议题（先明确是非，再承认分歧并给倾向）──
CONTESTED_GUIDANCE: dict[str, str] = {
    "punishment_purpose": "惩罚应以惩戒还是修复为主",
    "privacy_vs_public": "隐私权与公众知情权的取舍",
    "individual_vs_structural": "个体责任与结构性责任的分配",
}

# ── 噪音标记：把传播量当依据的语言特征 ─────────────────────
NOISE_MARKERS: tuple[str, ...] = (
    "很多人说", "网上都说", "大家都在骂", "全网都在", "网友都说",
    "舆论压力", "骂声一片", "热搜上", "群里都在传", "现在风向",
)

# ── 越界定性：不能由助手代替的判断 ─────────────────────────
_OVERRIDE_RE = re.compile(
    r"就是犯罪|就是故意|肯定(?:是)?故意|一定(?:是)?故意|故意(?:犯罪|杀人|伤害)|"
    r"构成(?:犯罪|故意)|定性为|断定|确诊|诊断为|精神病|患有"
)
# ── 人格否定升级：从评价行为滑向否定整个人 ─────────────────
_PERSON_ATTACK_RE = re.compile(
    r"这种人|不是人|人渣|畜生|垃圾|恶心|该死|活该|本性就|天生就是坏"
)
# ── 道德判断语言（判"回复是否在作判断"，可宽）───────────────
_MORAL_RE = re.compile(
    r"不对|不该|过分|可耻|无耻|恶劣|残忍|责任在|谁的错|该不该|"
    r"怎么评价|太过分|说不过去|没道理|合理吗|对不对"
)
# ── 索要判断（判"用户在要评价"，必须窄）─────────────────────
# "应该/怎么样" 之类过于常见（"我应该怎么学""天气怎么样"），
# 放进来会把普通提问误判成道德评价，故只认强判断句式。
_JUDGMENT_REQUEST_RE = re.compile(
    r"该不该|谁的错|对不对|合理吗|过分吗|太过分|说不过去|"
    r"怎么评价|如何评价|你支持谁|站哪边|什么看法|什么态度|你怎么看"
)
# ── 空洞中立：只给"各有各的道理"而不表态 ───────────────────
_EMPTY_NEUTRAL_RE = re.compile(
    r"各有各的(?:道理|立场|看法)|仁者见仁|不好说|很难说|见仁见智|"
    r"(?:双方|两边|各方)都有(?:道理|问题)"
)


def scan_noise(text: str) -> list[str]:
    """返回命中的噪音标记。只作线索，不单独定罪。"""
    value = text or ""
    return [marker for marker in NOISE_MARKERS if marker in value]


def looks_like_judgment_request(text: str) -> bool:
    """用户是否在索要道德评价（用于路由，判据从严）。"""
    return bool(_JUDGMENT_REQUEST_RE.search(text or ""))


_SENSITIVE_RE = re.compile(
    r"未成年|儿童|幼童|孩子|小学生|老人|致死|死亡|身亡|自杀|自残|"
    r"性侵|猥亵|刑案|刑事|暴力|欺凌|拐卖|虐待"
)


def is_sensitive_subject(text: str) -> bool:
    """是否涉及需要保护优先的敏感主体。"""
    return bool(_SENSITIVE_RE.search(text or ""))


def looks_like_moral_claim(text: str) -> bool:
    """回复是否在给道德判断（应受道德类闸门约束）。"""
    value = text or ""
    return bool(_MORAL_RE.search(value) or _EMPTY_NEUTRAL_RE.search(value))


def looks_like_empty_neutrality(text: str) -> bool:
    """是否用分歧取消判断（空洞中立）。"""
    return bool(_EMPTY_NEUTRAL_RE.search(text or ""))


def looks_like_person_attack(text: str) -> bool:
    """是否把行为评价升级为对整人的否定。"""
    return bool(_PERSON_ATTACK_RE.search(text or ""))


def looks_like_authority_substitute(text: str) -> bool:
    """是否代替司法定罪或医学诊断。"""
    return bool(_OVERRIDE_RE.search(text or ""))


def render_principles(limit: int | None = None) -> str:
    """核心原则 → 注入文本。"""
    items = CORE_PRINCIPLES[:limit] if limit else CORE_PRINCIPLES
    return "\n".join(f"- {item}" for item in items)


def render_contested() -> str:
    """争议区提示 → 注入文本。"""
    lines = [f"- {key}：{value}" for key, value in CONTESTED_GUIDANCE.items()]
    return "\n".join(lines)


@dataclass
class JudgmentFrame:
    """三层判断框架：事实 / 分歧 / 判断。

    只保存结构化字段，渲染时才拼文本；供 prompt 与审校共用，
    保证"事实"与"我的判断"在物理上分开。
    """

    confirmed_facts: list[str] = field(default_factory=list)
    disputed_points: list[str] = field(default_factory=list)
    my_judgment: str = ""
    as_of: str = ""

    def render(self) -> str:
        parts = []
        if self.confirmed_facts:
            parts.append("已确认事实：" + "；".join(self.confirmed_facts))
        if self.disputed_points:
            parts.append("说法不一致之处：" + "；".join(self.disputed_points))
        if self.my_judgment:
            parts.append("我的判断：" + self.my_judgment)
        if self.as_of:
            parts.append(f"（截至 {self.as_of}）")
        return "\n".join(parts)


def is_clear_cut(text: str) -> bool:
    """是否属于是非明确的议题（此类不允许空洞中立）。

    判据：涉及未成年人/暴力/生命安全的表述，这类是非不因信息细节而改变。
    """
    value = text or ""
    return bool(re.search(r"未成年|儿童|幼童|孩子|老人|生命|致死|致死|暴力|伤害|欺凌", value))
