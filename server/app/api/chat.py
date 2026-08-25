"""聊天接口：记忆检索注入 + LLM 编排 + 记录归档。"""
import re
from fastapi import APIRouter
from pydantic import BaseModel

from app.core import llm, memory
from app.services import worklog
from app.services.profile import get_profile_injection

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

    # 1) 检索相关记忆并注入
    mems = await memory.search(msg)
    injections = memory.format_injection(mems)
    profile = get_profile_injection()

    system = SYSTEM_PROMPT.replace("{injections}", injections or "（暂无相关记忆）")
    system = system.replace("{profile}", profile or "（画像未建立，通过对话逐步了解用户）")

    # 2) 记录用户消息（先入库，LLM 摘要整合由定时任务完成）
    await memory.write_message("user", msg)

    # 3) 调 LLM
    reply = await llm.chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": msg},
        ]
    )

    # 4) 记录回复 + 提升被引用记忆的重要性
    await memory.write_message("assistant", reply)
    if mems:
        memory.bump_importance([m["id"] for m in mems])

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
