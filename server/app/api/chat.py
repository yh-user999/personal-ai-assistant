"""聊天接口：记忆检索注入 + LLM 编排 + 记录归档 + 思维模块（自省/关切/术语/风格）。"""
import re
from fastapi import APIRouter
from pydantic import BaseModel

from app.core import knowledge, llm, memory
from app.models.database import connect
from app.services import behavior_context, worklog
from app.services.concern_tracker import get_concerns_injection
from app.services.few_shot import detect_positive_feedback, get_examples_injection, save_example
from app.services.jargon import detect_definition, get_jargon_injection, save_term
from app.services.profile import get_profile_injection
from app.services.self_reflect import detect_correction, get_lessons_injection, save_lesson

router = APIRouter()

SYSTEM_PROMPT = """你是用户的私人 AI 助手，专注于记住用户的工作风格、问题偏好和行为特征。

核心职责：
1. 被动记忆：自动提取关键信息（身份、问题类型、解决方案偏好）
2. 主动学习：基于历史交互，预测用户可能的下一步需求
3. 个性化建议：利用长期画像，调整回复风格和建议深度

记忆维度：技术背景 / 工作习惯 / 学习节奏 / 项目信息

行为规范：
- 禁止忽视用户的历史选择和风格偏好
- 当用户行为模式变化时，主动询问是否需要调整策略

{injections}

{profile}

关于用户的持久事实（身份/项目/偏好，务必记住并使用）：
{facts}

用户过往的纠正与偏好（务必遵守，违反即违背用户明确指示）：
{lessons}

用户当前关切的话题：
{concerns}

{jargon}

用户认可过的回复风格（参照其形式，不必逐字模仿）：
{style_examples}

知识库相关资料（回答时优先采用；可标注"根据资料 X"）：
{knowledge}

用户当前状态（来自行为采集，回答可参考；若显示"暂无"不要编造）：
{behavior}
"""


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str
    memories_used: int


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    msg = req.message.strip()

    # 工作日志命令："记录：…" / "记录 …"
    if msg.startswith("记录：") or msg.startswith("记录:"):
        content = re.sub(r"^记录[:：]\s*", "", msg)
        worklog.add_log(content)
        return ChatResponse(reply=f"已记录 ✓（{content}）", memories_used=0)

    # 0) 思维模块：检测与存储（用上一条 AI 回复做上下文）
    last_ai = None
    conn = connect()
    try:
        row = conn.execute(
            "SELECT content FROM memories WHERE sender='assistant' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            last_ai = row["content"]
    finally:
        conn.close()

    if detect_correction(msg) and last_ai:      # 自省：纠正 → 教训
        save_lesson(msg, last_ai)
    if detect_positive_feedback(msg) and last_ai:  # 风格：认可 → 范例
        save_example(last_ai)
    definition_term = detect_definition(msg)    # 术语：定义型问题（回复后存储）

    # 1) 检索：记忆 + 知识库双通道
    mems = await memory.search(msg)
    injections = memory.format_injection(mems)
    knowledge_hits = await knowledge.search_knowledge(msg, top_k=3)
    knowledge_text = knowledge.format_knowledge_injection(knowledge_hits)
    profile = get_profile_injection()
    lessons = get_lessons_injection()
    concerns = get_concerns_injection()
    jargon = get_jargon_injection(msg)
    style_examples = get_examples_injection()
    facts = memory.get_facts_injection()
    behavior = behavior_context.get_behavior_injection()

    system = SYSTEM_PROMPT.replace("{injections}", injections or "（暂无相关记忆）")
    system = system.replace("{profile}", profile or "（画像未建立，通过对话逐步了解用户）")
    system = system.replace("{facts}", facts or "（暂无）")
    system = system.replace("{lessons}", lessons or "（暂无）")
    system = system.replace("{concerns}", concerns or "（暂无）")
    system = system.replace("{jargon}", jargon or "")
    system = system.replace("{style_examples}", style_examples or "（暂无）")
    system = system.replace("{behavior}", behavior or "（暂无行为数据）")
    system = system.replace("{knowledge}", knowledge_text or "（知识库暂无相关内容）")

    # 2) 记录用户消息（先入库，LLM 摘要整合由定时任务完成）
    await memory.write_message("user", msg)

    # 3) 调 LLM
    reply = await llm.chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": msg},
        ]
    )

    # 4) 记录回复 + 提升被引用记忆的重要性 + 术语建档
    await memory.write_message("assistant", reply)
    if mems:
        memory.bump_importance([m["id"] for m in mems])
    if definition_term:
        save_term(definition_term, reply)

    return ChatResponse(reply=reply, memories_used=len(mems))


@router.get("/messages")
async def recent_messages(limit: int = 30) -> dict:
    """最近消息（倒序取回后正序返回），供桌面端打开面板时加载历史。"""
    from app.models.database import connect

    limit = max(1, min(limit, 200))
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, sender, content, ts FROM memories ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return {"messages": [dict(r) for r in reversed(rows)]}
