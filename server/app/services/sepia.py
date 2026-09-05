"""Sepia 小说生成与审校规则。

本模块只承载短规则块和确定性表层扫描，不读数据库、不调用 LLM。
Sepia 的叙事/话语建议属于软约束；表层污染属于可由正文直接证明的硬性预检。
"""
from __future__ import annotations

import re
from collections.abc import Iterable

GENERATION_GUIDE = """【Sepia 小说生成规则】
这是叙事方法约束，不是新的世界观设定；权威设定与前情事实优先于文风建议。
叙事层：让场景中的行动、选择和后果推动正文；不要把主题、寓意或人物成长直接讲给读者。因果链可以有一处留白，允许一个暂不回收的细节；关键信息不要一次性 briefing，按场景需要逐步释放，保留一次能回看前文的重context reveal。结尾停在具体动作、信息或情绪落点，不要替人物总结人生道理。
话语层：保持当前视角、时态和叙述距离；对白先服务于人物欲望、关系和冲突，不写成哲学辩论。动作、对白、心理和普通叙述交替，心理与感官描写适量，避免每次情绪都套用“心口发紧、空气凝固”一类模板。句式长短要有变化，保留少量普通、直白、没有修辞的句子；不要为了“像人”故意制造错别字或生硬口语。
表层边界：只输出小说正文。不得输出标题、章节总结、提纲、列表、Markdown、代码块、字数说明、写作分析、道歉、模型自述、读者提示或“如果需要我继续”等元话语。不要重写已经给出的正文，只从当前段落之后自然接写。"""


REVIEW_GUIDE = """【Sepia 小说审校规则】
Sepia 只审三类问题，并与逻辑、时间线、动机、称谓、设定五维问题分开：
叙事：场景是否在推进，信息是否过早解释，转折/结局是否过于顺滑，是否存在机械式全收束或主题直说；
话语：视角/时态是否漂移，对白是否变成作者讲道，心理和感官是否模板化，句式/段落节奏是否整齐单调；
表层：正文是否混入章节尾标记、标题/列表/Markdown、代码块或 AI 元话语。
只报告正文中有明确引句支持的问题；风格偏好、尚未确认的设定和“我个人不喜欢”不算硬问题。每条给 type（叙事|话语|表层）、quote、problem、suggestion。没有证据就不要报，不要为了凑数改写正文。"""


_MAX_FINDINGS = 12
_MAX_EXCERPT = 80

# 公开规则表：章节分析和其他调用方可以复用同一套表层判据。
SURFACE_RULES: dict[str, re.Pattern[str]] = {
    "章节尾标记": re.compile(
        r"^\s*(?:本章完|第[一二三四五六七八九十百千万两0-9]+章完|全文完|完。|完)\s*$"
    ),
    "AI元话语": re.compile(
        r"写完了|你看看|参考参考|以下是|希望.{0,12}符合|如果需要.{0,12}继续|"
        r"字数[约大]|需要我.{0,8}修改|如有.{0,6}问题.{0,8}提出|"
        r"如需继续|如果你想继续|需要我再写|正文如下|续写如下|章节正文如下|以上是"
    ),
    "标题式解释": re.compile(
        r"^\s*(?:以下是|下面是)?(?:本章|章节|正文|续写内容|故事内容)"
        r"(?:说明|正文|内容|梗概|提纲)?\s*[:：]"
    ),
    "Markdown格式": re.compile(
        r"^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s+)|"
        r"(?:\*\*[^*\n]+\*\*|(?<!\*)\*[^*\n]+\*(?!\*)|`[^`\n]+`)"
    ),
    "代码块": re.compile(r"```"),
}


def _excerpt(line: str) -> str:
    line = re.sub(r"\s+", " ", line.strip())
    return line[:_MAX_EXCERPT]


def detect_surface_violations(text: str) -> list[tuple[str, str]]:
    """确定性扫描正文表层污染，返回 ``[(kind, excerpt), ...]``。

    每个非空行最多报告一个类别，按正文出现顺序保留，最多 12 条；代码块只报告
    围栏行，避免把代码块内部的普通文本误当成正文违规。
    """
    if not text:
        return []

    findings: list[tuple[str, str]] = []
    in_code = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        if SURFACE_RULES["代码块"].search(line):
            findings.append(("代码块", _excerpt(line)))
            if line.count("```") % 2:
                in_code = not in_code
            if len(findings) >= _MAX_FINDINGS:
                break
            continue
        if in_code:
            continue

        kind: str | None = None
        # 顺序与旧残留检测一致：章节尾标记优先于元话语，Markdown 规则最后兜底。
        for candidate in ("章节尾标记", "AI元话语", "标题式解释", "Markdown格式"):
            if SURFACE_RULES[candidate].search(line):
                kind = candidate
                break
        if kind:
            findings.append((kind, _excerpt(line)))
            if len(findings) >= _MAX_FINDINGS:
                break

    return findings


def format_surface_findings(
    findings: Iterable[tuple[str, str]],
    *,
    prefix: str = "表层预检：",
) -> str:
    """把表层结果格式化为 QQ 可读的纯文本；空结果返回空串。"""
    rows = list(findings)[:_MAX_FINDINGS]
    if not rows:
        return ""
    lines = [prefix]
    lines.extend(f"  [{kind}] {excerpt[:_MAX_EXCERPT]}" for kind, excerpt in rows)
    return "\n".join(lines)


def build_generation_block() -> str:
    """返回可直接拼入正文生成 prompt 的短规则块。"""
    return GENERATION_GUIDE


def build_review_block() -> str:
    """返回可直接拼入章节审校 prompt 的短规则块。"""
    return REVIEW_GUIDE
