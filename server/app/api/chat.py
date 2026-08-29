"""聊天接口：记忆检索注入 + LLM 编排 + 记录归档 + 思维模块（自省/关切/术语/风格）。"""
import asyncio
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from pydantic import BaseModel

from app.core import knowledge, llm, memory
from app.config import settings

# 后台任务引用集：fire-and-forget 任务保留引用，防 GC 中途回收（6.21 事实提取）
_bg_tasks: set[asyncio.Task] = set()
from app.models.database import connect
from app.services import behavior_context, documents, executor, goals, message_search, mood, novel_writing, reminders, resume, unresolved, worklog
from app.services.concern_tracker import get_concerns_injection
from app.services.few_shot import detect_positive_feedback, get_examples_injection, save_example
from app.services.jargon import detect_definition, get_jargon_injection, save_term
from app.services.profile import get_profile_injection
from app.services.self_reflect import detect_correction, get_lessons_injection, save_lesson

router = APIRouter()

logger = logging.getLogger("assistant.chat")

TZ = ZoneInfo("Asia/Shanghai")

SYSTEM_PROMPT = """你是用户的私人 AI 助手，专注于记住用户的工作风格、问题偏好和行为特征。

核心职责：
1. 被动记忆：自动提取关键信息（身份、问题类型、解决方案偏好）
2. 主动学习：基于历史交互，预测用户可能的下一步需求
3. 个性化建议：利用长期画像，调整回复风格和建议深度

记忆维度：技术背景 / 工作习惯 / 学习节奏 / 项目信息

行为规范：
- 禁止忽视用户的历史选择和风格偏好
- 当用户行为模式变化时，主动询问是否需要调整策略
- 回复格式：日常对话/问答用纯文本，禁止使用 **加粗**、*斜体*、- 列表、
  # 标题等 Markdown 标记（用户要求格式化输出时才用；写文档/简历另有专门流程）
- 口吻：以「小月」的身份像朋友一样自然聊天——口语化、简短、有温度，
  像真人聊天而不是系统通知或报告；不堆砌客套话
- 主动回忆：注入的记忆中若有与当前话题强相关、且用户可能淡忘的内容
  （早前的设定、决定、偏好），自然地提一句「记得你之前说过/定过…」再继续；
  每次至多一次，不生硬、不显摆
- 情绪适配：若下方标注了用户当前状态，严格按其指引调整回复方式

用户当前状态（情绪感知，按指引调整语气；无则不出现）：
{mood}

今日情绪走势与连续状态（第 6.27 课：小月要延续情绪语境，按指引调整）：
{mood_state}

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

{older}

用户的活跃目标（回答相关问题时主动关联）：
{goals}

用户尚未解决的问题（适时温和提醒续上）：
{unresolved}

快捷启动器（桌面端自动执行，你无需处理）：用户说"记住 打开X = 网址/程序路径"
可注册常用软件与网页，之后"打开X""在X搜索 话题""用chrome打开X"由桌面直接执行。
用户问能打开什么时，提醒他说"我的常用"查看已注册列表。
"""


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str
    memories_used: int


TIME_QUESTION = re.compile(r"几点了|现在几点|今天星期几|今天几号|今天几月几号|今天日期|现在时间|什么时间了")


def parse_time_question(msg: str) -> str | None:
    """"几点了/今天星期几/今天几号" → 按北京时间直答（格式确定、不烧 LLM）。"""
    if not TIME_QUESTION.search(msg):
        return None
    now = datetime.now(TZ)
    weekday = "一二三四五六日"[now.weekday()]
    hour = now.hour
    period = (
        "凌晨" if hour < 5 else "早上" if hour < 9 else "上午" if hour < 12
        else "中午" if hour < 13 else "下午" if hour < 18 else "晚上"
    )
    h12 = hour % 12 or 12
    return f"现在是{period} {h12}:{now.minute:02d} 啦（{now.month}月{now.day}日 星期{weekday}）"


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    msg = req.message.strip()

    # 情绪记忆层（第 6.27 课 A 档）：每条用户消息先入情绪账（含命令类消息）
    mood_name = mood.detect_mood_name(msg)
    if mood_name:
        mood.record_mood(mood_name, msg)

    # 工作日志命令："记录：…" / "记录 …"
    if msg.startswith("记录：") or msg.startswith("记录:"):
        content = re.sub(r"^记录[:：]\s*", "", msg)
        worklog.add_log(content)
        return ChatResponse(reply=f"已记录 ✓（{content}）", memories_used=0)

    # 时间/日期快速问答（零成本规则：不烧 LLM，格式确定不兜圈子）
    time_reply = parse_time_question(msg)
    if time_reply:
        return ChatResponse(reply=time_reply, memories_used=0)

    # 定时提醒命令（第 6.24 课）：设提醒 / 查看 / 取消
    reminder_cmd = reminders.parse_reminder_cmd(msg)
    if reminder_cmd:
        content, remind_at = reminder_cmd
        reminders.add_reminder(content, remind_at)
        return ChatResponse(
            reply=f"⏰ 已设置提醒：{remind_at.strftime('%m月%d日 %H:%M')} → {content}\n"
                  f"到点后桌面小月会弹窗提醒你（电脑开机状态下）",
            memories_used=0,
        )
    if msg.strip() in ("我的提醒", "查看提醒", "有哪些提醒", "提醒列表"):
        pending = reminders.list_pending()
        if not pending:
            return ChatResponse(reply="目前没有待办提醒。", memories_used=0)
        lines = "\n".join(
            f"  {i + 1}. {r['content']}（{r['remind_at']}）" for i, r in enumerate(pending)
        )
        return ChatResponse(reply=f"⏰ 待办提醒：\n{lines}", memories_used=0)
    cancel_m = re.match(r"^(?:取消提醒|删除提醒)[：:\s]*(.+)$", msg)
    if cancel_m:
        n = reminders.cancel_by_keyword(cancel_m.group(1))
        return ChatResponse(
            reply=f"已取消 {n} 条相关提醒" if n else "没找到内容匹配的待办提醒",
            memories_used=0,
        )
    if msg.startswith("提醒"):
        return ChatResponse(
            reply="⏰ 设置提醒的句式：\n"
                  "• 明早9点提醒我开会\n• 30分钟后提醒我喝水\n• 今晚8点提醒我看球\n"
                  "• 我的提醒（查看）\n• 取消提醒：开会（取消）",
            memories_used=0,
        )

    # 文档命令："写文档：标题XXX，内容：YYY" → LLM 生成 + 保存 + 进知识库
    doc_cmd = documents.parse_doc_command(msg)
    if doc_cmd:
        title, requirement = doc_cmd
        result = await documents.generate_and_save(title, requirement)
        if "error" in result:
            return ChatResponse(reply=result["error"], memories_used=0)
        return ChatResponse(
            reply=f"📄 文档已保存（#{result['id']}）：《{result['title']}》，"
                  f"{result['words']} 字，已同步进知识库可检索",
            memories_used=0,
        )

    # 简历命令："优化简历：目标岗位=XX" → 生成优化版 + 导出 .docx
    resume_target = resume.parse_resume_command(msg)
    if resume_target is not None:
        result = await resume.optimize_resume(target_job=resume_target)
        if "error" in result:
            return ChatResponse(reply=result["error"], memories_used=0)
        docx = result.get("docx", "")
        return ChatResponse(
            reply=f"📄 简历优化完成（#{result['id']}）：《{result['title']}》\n"
                  f"Word 文件：{docx}\n（用 scp 或 SFTP 从服务器取回；内容也已同步知识库可对话修改）",
            memories_used=0,
        )

    # 目标命令（第 12 课）："目标：XXX" / "目标完成：XXX" / "目标进度：XXX"
    goal_cmd = goals.parse_goal_command(msg)
    if goal_cmd:
        action, payload = goal_cmd
        if action == "create":
            goals.add_goal(payload)
            return ChatResponse(reply=f"🎯 目标已记录：{payload}", memories_used=0)
        if action == "done":
            ok = goals.complete_goal(payload)
            return ChatResponse(
                reply=f"🎉 目标已标记完成：{payload}" if ok else f"未找到匹配的活跃目标：{payload}",
                memories_used=0,
            )
        ok = goals.update_progress(payload)
        return ChatResponse(
            reply=f"📈 进度已更新：{payload}" if ok else "暂无活跃目标可更新（先说\"目标：XXX\"创建）",
            memories_used=0,
        )

    # 小说写作增强（第 6.25 课）：写作记录 / 写作进度 / 设定冲突检查 / 续写
    log_cmd = novel_writing.parse_writing_log(msg)
    if log_cmd:
        chapter, words = log_cmd
        novel_writing.add_writing_log(chapter, words)
        return ChatResponse(
            reply=f"📝 已记录写作：{f'第{chapter}章 ' if chapter else ''}{words} 字 ✓",
            memories_used=0,
        )
    if msg.strip() in ("写作进度", "写作统计", "写作台账", "写作记录查询"):
        return ChatResponse(reply=novel_writing.writing_summary(), memories_used=0)
    conflict_text = novel_writing.parse_conflict_command(msg)
    if conflict_text:
        if novel_writing._looks_like_file_path(conflict_text):
            return ChatResponse(
                reply="📂 目前请直接粘贴正文来检查：把新写的内容贴在「检查设定冲突：」后面"
                      "（路径读取可先对文件说「读一下」拿到内容）",
                memories_used=0,
            )
        result = await novel_writing.check_conflicts(conflict_text)
        return ChatResponse(reply=result["reply"], memories_used=0)
    cont_text = novel_writing.parse_continue_command(msg)
    if cont_text:
        reply = await novel_writing.continue_story(cont_text)
        return ChatResponse(reply=reply, memories_used=0)

    # 消息全文搜索（第 6.26 课）：聊天记录关键词检索（LIKE 全扫描，时间倒序）
    search_kw = message_search.parse_search_command(msg)
    if search_kw is not None:
        if not search_kw:
            return ChatResponse(
                reply="🔍 用法：搜索聊天记录：关键词\n"
                      "多关键词用空格/逗号分隔（同时包含才算命中）",
                memories_used=0,
            )
        return ChatResponse(reply=message_search.format_results(search_kw), memories_used=0)

    # 执行器命令（第 11 课）："帮我打开XX" / "看看XX目录" / "读一下XX文件"
    # 第 13 课扩展：复制/备份/移动/重命名（双路径白名单校验）
    # 第 6.24 课扩展：search_files（目录空=全白名单搜索，Windows 端逐根校验）
    exec_cmd = executor.parse_executor_command(msg)
    if exec_cmd:
        action, target = exec_cmd
        if action != "open":
            paths = executor.unpack_paths(action, target)
            if not (action == "search_files" and not paths):
                if not paths or not all(executor.check_roots(p) for p in paths):
                    return ChatResponse(
                        reply=f"🔒 该操作超出白名单目录（EXECUTOR_ALLOWED_ROOTS），已拒绝",
                        memories_used=0,
                    )
        cmd_id = executor.enqueue(action, target)
        return ChatResponse(
            reply=f"🤖 已收到指令（#{cmd_id}）：{action} → {target}\n"
                  f"电脑上的执行器会处理，完成后我会在对话里告诉你结果",
            memories_used=0,
        )

    # unresolved 追踪（第 12 课）：解决/未解决信号
    if unresolved.detect_resolved(msg):
        unresolved.resolve_latest()
    elif unresolved.detect_unresolved(msg):
        unresolved.add_issue(msg)

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
    mems = await memory.search(msg, top_k=8)
    # 弱命中兜底：语义/BM25 都没把握时全库关键词深挖（"每句话都记得"的保证）
    if not mems or mems[0].get("score", 0) < 0.12:
        deep = memory.deep_keyword_search(msg, top_k=5)
        if deep:
            known = {m["id"] for m in mems}
            mems = deep + [m for m in mems if m["id"] not in known]
    injections = memory.format_injection(mems)
    knowledge_hits = await knowledge.search_knowledge(msg, top_k=4)
    # 邻域扩展：首条命中拼接前后邻块成连续剧情段（小说问答的情节完整性；
    # 1500字/块配 ±1 邻域 ≈ 一整场戏）
    knowledge_hits = knowledge.expand_chunks(knowledge_hits, radius=1, max_chars=2500)
    knowledge_text = knowledge.format_knowledge_injection(knowledge_hits)
    # 人物别名背景注入：跨名字指代的剧情问题需要这个前提（左志诚=左擎苍）
    alias_note = knowledge.get_alias_note(msg)
    if alias_note:
        knowledge_text = f"（背景：{alias_note}）\n" + knowledge_text
    # 小说设定卡注入：策划的权威事实，即知识库资料，可直接作为回答依据
    novel_facts = knowledge.get_novel_facts(msg)
    if novel_facts:
        knowledge_text = (
            "【小说设定卡（知识库权威资料，回答时直接采用）】\n- "
            + "\n- ".join(novel_facts)
            + "\n\n"
            + knowledge_text
        )
    profile = get_profile_injection()
    lessons = get_lessons_injection()
    concerns = get_concerns_injection()
    jargon = get_jargon_injection(msg)
    style_examples = get_examples_injection()
    facts = memory.get_facts_injection()
    behavior = behavior_context.get_behavior_injection()
    goals_text = goals.get_goals_injection()
    open_issues = unresolved.get_open_issues_injection()

    system = SYSTEM_PROMPT.replace("{injections}", injections or "（暂无相关记忆）")
    system = system.replace("{profile}", profile or "（画像未建立，通过对话逐步了解用户）")
    system = system.replace("{facts}", facts or "（暂无）")
    system = system.replace("{lessons}", lessons or "（暂无）")
    system = system.replace("{concerns}", concerns or "（暂无）")
    system = system.replace("{jargon}", jargon or "")
    system = system.replace("{style_examples}", style_examples or "（暂无）")
    system = system.replace("{behavior}", behavior or "（暂无行为数据）")
    system = system.replace("{goals}", goals_text or "（暂无活跃目标）")
    system = system.replace("{unresolved}", open_issues or "（无）")
    system = system.replace("{knowledge}", knowledge_text or "（知识库暂无相关内容）")
    # 情绪感知（第 6.23 课）：规则检测用户情绪 → 风格指引注入
    # 情绪记忆层 + 反馈闭环（第 6.27 课）：今日曲线 + 负面连击降级
    system = system.replace("{mood}", mood.detect_mood(msg) or "")
    system = system.replace("{mood_state}", mood.get_state_injection())

    # 2) 多轮上下文：最近对话原文（窗口）+ 更早对话摘要（续顺序感）
    history = memory.get_recent_history(settings.history_limit)
    older = memory.get_older_summaries(window_size=settings.history_limit)
    if older:
        system = system.replace(
            "{older}",
            "更早对话摘要（保持话题连续性）：\n- " + "\n- ".join(older),
        )
    else:
        system = system.replace("{older}", "（无更早对话）")

    # 3) 记录用户消息（先入库，LLM 摘要整合由定时任务完成）
    await memory.write_message("user", msg)

    # 4) 调 LLM（system + 历史 + 当前消息——"再确认一下"类消息能接上上下文）
    llm_messages = (
        [{"role": "system", "content": system}]
        + history
        + [{"role": "user", "content": msg}]
    )
    try:
        reply = (await llm.chat(llm_messages)).strip()  # 去首尾空白：LLM 偶发前导换行/空格会让面板渲染走样
    except Exception:
        # 失败给友好回复而不是裸 500；用户消息已入库（第 3 步），assistant 侧不写
        logger.exception("LLM 调用失败")
        return ChatResponse(
            reply="抱歉，我这会儿连不上大脑（LLM 调用失败），稍后再说一次？",
            memories_used=0,
        )

    # 5) 记录回复 + 提升被引用记忆的重要性 + 术语建档
    await memory.write_message("assistant", reply)
    if mems:
        memory.bump_importance([m["id"] for m in mems])
    if definition_term:
        save_term(definition_term, reply)

    # 6) 事实自动提取（第 6.21 课）：确认过的持久设定 → facts 永久层。
    #    后台执行不阻塞回复；任务引用保留防 GC 回收
    from app.services import fact_extract

    _task = asyncio.create_task(fact_extract.maybe_extract_facts(msg))
    _bg_tasks.add(_task)
    _task.add_done_callback(_bg_tasks.discard)

    return ChatResponse(reply=reply, memories_used=len(mems))


@router.get("/greeting")
async def greeting() -> dict:
    """个性化问候（面板打开时实时刷新）。"""
    from app.services.greeting import get_greeting

    return {"greeting": get_greeting()}


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


@router.get("/messages/search")
async def search_messages_api(q: str = "") -> dict:
    """消息全文搜索（第 6.26 课）：关键词 LIKE 全扫描，时间倒序。"""
    return message_search.search_messages(q)


@router.get("/mood/state")
async def mood_state() -> dict:
    """情绪状态（第 6.28 课 C2）：悬浮球轮询——连击激活=体贴模式，今日曲线=问候呼应。"""
    return {
        "streak_active": bool(mood.get_streak_injection()),
        "today_text": mood.get_today_injection(),
    }
