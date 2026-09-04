"""MCP Prompt 模板：只组装任务说明，不直接调用 LLM。"""
from __future__ import annotations

from typing import Any

from .schemas import bounded_text


def review_novel_chapter(chapter_text: str, focus: str = "剧情连贯、人物动机、伏笔与节奏") -> str:
    chapter = bounded_text(chapter_text, name="chapter_text", max_chars=12000)
    focus_text = bounded_text(focus, name="focus", max_chars=300)
    return (
        "请审阅下面的小说章节。先区分原文事实与推断，再按以下关注点给出问题、证据和可执行修改建议；"
        "不要续写，不要擅自改设定。\n\n"
        f"关注点：{focus_text}\n\n章节原文：\n{chapter}"
    )


def summarize_week(week_context: str, goals_context: str = "（未提供目标资料）") -> str:
    context = bounded_text(week_context, name="week_context", max_chars=12000)
    goals = bounded_text(goals_context, name="goals_context", max_chars=4000)
    return (
        "请基于给定资料生成周总结。只使用资料中存在的事实，明确区分完成项、进展、阻塞与下周行动；"
        "数据不足处直接标注，不得编造。\n\n"
        f"本周资料：\n{context}\n\n目标资料：\n{goals}"
    )


def analyze_project_progress(project_context: str, question: str = "当前进度、主要风险和下一步是什么？") -> str:
    context = bounded_text(project_context, name="project_context", max_chars=12000)
    question_text = bounded_text(question, name="question", max_chars=500)
    return (
        "请分析项目进展。结论必须引用所给资料，按“已完成 / 进行中 / 阻塞与风险 / 下一步”组织；"
        "无法确认的内容标为未知，不要替用户作高风险决策。\n\n"
        f"分析问题：{question_text}\n\n项目资料：\n{context}"
    )


def register_prompts(server: Any) -> None:
    server.prompt(
        name="review_novel_chapter",
        description="审阅小说章节的连贯性、人物动机、伏笔与节奏",
    )(review_novel_chapter)
    server.prompt(
        name="summarize_week",
        description="根据已有周资料和目标生成事实约束的周总结",
    )(summarize_week)
    server.prompt(
        name="analyze_project_progress",
        description="分析项目进度、风险、阻塞与下一步",
    )(analyze_project_progress)


__all__ = ["register_prompts"]
