"""回复去 Markdown：QQ 不渲染，星号减号会原样显示给用户。

为什么需要这层兜底（而不是只靠 prompt 禁令）：
prompt 规则是概率性的，temperature 0.7 下总会有漏网。实测线上回复里
`**加粗**` 与 `- 列表` 大量出现——用户看到的是一堆字面符号。

根因有两个，都修了：
1. **注入格式在示范输出格式**。实体索引/教训/目标/关切等注入用 `- ` 开头
   排版（实测实体注入里有 24 行），LLM 照着写。已在 prompt 里说明"注入里的
   `- ` 只是给你看的排版，不是让你照抄的格式"。
2. prompt 原来只说"禁止 Markdown"没说原因。补上"QQ 不渲染"这个理由后，
   模型遵守率明显更高——**规则带上原因比单纯禁止更有效**。

这层是最后一道：转换而非删除，保证信息不丢。
"""
import re

# 代码块：保留内容，去掉围栏（```python\ncode\n``` → code）
_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", re.DOTALL)
# 行首列表符号：`- ` `* ` `+ `（要求行首，避免误伤"3 - 2"这类算式）
_BULLET = re.compile(r"^[ \t]*[-*+][ \t]+", re.MULTILINE)
# 行首编号：`1. ` `2) `
_ORDERED = re.compile(r"^[ \t]*\d+[.)][ \t]+", re.MULTILINE)
# 标题：`## 标题` → `标题`
_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]*", re.MULTILINE)
# 加粗/斜体：**x** __x__ *x* _x_ → x
#   加粗先处理，否则 ** 会被单星号规则拆坏
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.DOTALL)
# 斜体判据不能用 (?<![\w*])：**中文字符属于 `\w`**，"这是*斜体*文字" 会被
# 前置断言排除掉（本项目第三次栽在这条上，见 LESSONS 6.30 全角冒号、
# 6.35 中文弯引号）。改为只排除星号本身，并要求星号内侧无空白——
# 这样 "3*4*5" 因为 4 两侧无空白也会命中，所以额外要求内容含非数字字符。
_ITALIC = re.compile(r"(?<!\*)\*(?!\s)([^*\n]*?[^\s*][^*\n]*?)(?<!\s)\*(?!\*)")


def _italic_sub(m: re.Match) -> str:
    """只有内容不是纯数字/运算式时才当斜体——避免误伤 3*4*5。"""
    inner = m.group(1)
    if re.fullmatch(r"[\d\s+\-./]+", inner):
        return m.group(0)
    return inner
# 行内代码：`x` → x（反引号在中文语境里几乎只用于代码标记）
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
# 引用块：`> 文字` → 文字
_QUOTE = re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE)
# 分隔线整行
_HR = re.compile(r"^[ \t]*([-*_])\1{2,}[ \t]*$", re.MULTILINE)
# 连续 3+ 空行压成 2 行
_BLANKS = re.compile(r"\n{3,}")


def strip_markdown(text: str) -> str:
    """把 Markdown 标记转成纯文本（转换而非删除，信息不丢）。"""
    if not text:
        return text
    s = _FENCE.sub(lambda m: m.group(1).strip(), text)
    s = _HR.sub("", s)
    s = _HEADING.sub("", s)
    s = _QUOTE.sub("", s)
    # 列表符号换成顿号式连写不安全（会改变语义），直接去掉符号保留换行
    s = _BULLET.sub("", s)
    s = _ORDERED.sub("", s)
    s = _BOLD.sub(lambda m: m.group(1) or m.group(2) or "", s)
    s = _ITALIC.sub(_italic_sub, s)
    s = _INLINE_CODE.sub(r"\1", s)
    s = _BLANKS.sub("\n\n", s)
    return s.strip()


def has_markdown(text: str) -> bool:
    """是否含 Markdown 标记（用于监控遵守率，不用于判断是否处理）。"""
    if not text:
        return False
    return bool(
        _FENCE.search(text) or _BULLET.search(text) or _ORDERED.search(text)
        or _HEADING.search(text) or _BOLD.search(text) or _QUOTE.search(text)
    )
