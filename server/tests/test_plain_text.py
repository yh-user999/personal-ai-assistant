"""去 Markdown 测试：QQ 不渲染，星号减号会原样显示给用户。

背景：prompt 里早就禁了 Markdown，但实测线上回复大量出现 `**加粗**` 与
`- 列表`。两个根因——① 注入内容本身用 `- ` 排版（实体注入里有 24 行），
LLM 照抄；② 原来的禁令没说原因（"QQ 不渲染"）。这层是确定性兜底。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

from app.services.plain_text import has_markdown, strip_markdown

# ── 各类标记的转换 ────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("李羽的能力是**杀人变强**——每杀一个人", "李羽的能力是杀人变强——每杀一个人"),
    ("__也是加粗__", "也是加粗"),
    ("这是*斜体*文字", "这是斜体文字"),
    ("行内`代码`标记", "行内代码标记"),
    ("## 二级标题", "二级标题"),
    ("### 三级标题", "三级标题"),
    ("> 引用文字", "引用文字"),
])
def test_inline_markers_converted(raw, expected):
    assert strip_markdown(raw) == expected


def test_bullet_list_keeps_content():
    """列表符号去掉但内容与换行保留——不能把多项挤成一行改变语义。"""
    raw = "命丛如下：\n- 夜海：位于左眼\n- 白茫：位于肺部\n- 尸脉：改造躯体"
    out = strip_markdown(raw)
    assert out == "命丛如下：\n夜海：位于左眼\n白茫：位于肺部\n尸脉：改造躯体"


@pytest.mark.parametrize("marker", ["-", "*", "+"])
def test_all_bullet_markers(marker):
    assert strip_markdown(f"{marker} 条目") == "条目"


def test_ordered_list():
    raw = "1. 第一条\n2. 第二条\n3) 第三条"
    assert strip_markdown(raw) == "第一条\n第二条\n第三条"


def test_code_fence_keeps_code():
    raw = "示例：\n```python\nprint('hi')\n```\n完了"
    out = strip_markdown(raw)
    assert "print('hi')" in out
    assert "```" not in out
    assert "python" not in out, "语言标注应被去掉"


def test_horizontal_rule_removed():
    assert "---" not in strip_markdown("上文\n\n---\n\n下文")


def test_blank_lines_collapsed():
    assert strip_markdown("一段\n\n\n\n二段") == "一段\n\n二段"


# ── 不该误伤的情况 ────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "计算 5 - 3 等于 2",          # 减号在句中不是列表
    "范围是 3*4 的矩阵",           # 乘号
    "文件名 my_var_name 保留",     # 下划线在词中
    "命丛有夜海、白茫、尸脉",        # 顿号连写（我们期望的格式）
    "他说：这个不行。",
])
def test_no_false_positives(text):
    assert strip_markdown(text) == text, f"误伤: {text}"


def test_italic_not_triggered_by_multiplication():
    """`3*4*5` 不该被当成斜体。"""
    assert strip_markdown("算式 3*4*5 的结果") == "算式 3*4*5 的结果"


# ── has_markdown 判定 ────────────────────────────────────

@pytest.mark.parametrize("text,flag", [
    ("**加粗**", True),
    ("- 列表", True),
    ("1. 编号", True),
    ("## 标题", True),
    ("> 引用", True),
    ("```code```", True),
    ("纯文本没有标记", False),
    ("命丛有夜海、白茫", False),
    ("计算 5 - 3", False),
])
def test_has_markdown(text, flag):
    assert has_markdown(text) is flag


def test_strip_is_idempotent():
    """处理过的文本再处理一次不该变化。"""
    once = strip_markdown("**粗**\n- 项一\n- 项二")
    assert strip_markdown(once) == once
    assert not has_markdown(once)


def test_empty_and_none_safe():
    assert strip_markdown("") == ""
    assert strip_markdown(None) is None
    assert has_markdown("") is False
    assert has_markdown(None) is False


# ── 真实回复回归（线上实际出现过的形态）────────────────────

def test_real_reply_from_production():
    raw = (
        "按知识库实体索引，命丛清单如下：\n\n"
        "**神命丛（失传级）**\n- 夜海：位于左眼，修炼方法已失传\n\n"
        "**普通命丛**\n- 炎洞：放火类\n- 白茫（白芒）：位于肺部\n"
    )
    out = strip_markdown(raw)
    assert not has_markdown(out)
    # 信息不能丢
    for keep in ("夜海", "位于左眼", "炎洞", "放火类", "白茫", "神命丛", "普通命丛"):
        assert keep in out, f"信息丢失: {keep}"


def test_entity_injection_no_longer_uses_bullets(db):
    """注入内容自己不该用 `- ` 开头——它在示范 LLM 该怎么写。"""
    from app.models.database import connect
    from app.services import novel_entities as ne

    conn = connect()
    conn.execute(
        "INSERT INTO knowledge_chunks (doc_name, chunk_index, content, created_at, domain) "
        "VALUES ('测试小说', 1, '这个命丛被称之为夜海，位于左眼。', "
        "'2026-09-01T00:00:00+00:00', 'novel')"
    )
    conn.commit()
    ne.upsert_entity("测试小说", "夜海", "命丛", first_chunk=1)

    ctx = ne.build_entity_context("哪些命丛", book="测试小说")
    bullets = [ln for ln in ctx.splitlines() if ln.startswith("- ")]
    assert not bullets, f"注入仍用列表符号: {bullets}"
