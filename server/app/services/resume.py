"""简历优化：知识库简历原文 → 简历专家 prompt 重写 → 存文档 + 导出 .docx。

原则：只改写用户真实经历（不编造技能/经历）；按目标岗位匹配关键词；
STAR 法则量化成果。
"""
import re

from app.core import llm
from app.models.database import connect
from app.services import documents

RESUME_PROMPT = """你是资深简历优化专家，服务过阿里云/字节等公司的技术岗位招聘。
任务：根据用户简历原文与目标岗位，输出优化后的完整简历（Markdown）。

要求：
1. 只使用原文中的真实信息，【禁止编造】任何经历、技能、数据
2. 工作经历用 STAR 法则改写：情境-任务-行动-结果，量化成果
   （原文有数字才写数字，没有数字就写具体职责）
3. 技能按目标岗位 JD 关键词排序靠前（如有目标岗位）
4. 结构：基本信息 / 技能清单 / 工作经历 / 项目经验 / 证书 / 自我评价
5. 语言精炼专业，删除口语和冗余

输出格式：直接输出优化后的简历 Markdown（# 开头为姓名+岗位定位）。"""


RESUME_DOC_DEFAULT = "简历（脱敏版）"  # 知识库中的脱敏简历文档名（不含任何个人信息）


def _get_resume_text(doc_name: str = RESUME_DOC_DEFAULT) -> str:
    """从知识库取简历全部块（按块序拼接原文）。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT content FROM knowledge_chunks WHERE doc_name=? ORDER BY chunk_index",
            (doc_name,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return ""
    return "\n\n".join(r["content"] for r in rows)


def parse_resume_command(msg: str) -> str | None:
    """检测"优化简历/修改简历/美化简历"命令 → 目标岗位（无则空串）。"""
    m = re.match(r"^(?:优化简历|修改简历|美化简历|润色简历)[:：]?\s*(.*)$", msg.strip())
    if not m:
        return None
    rest = m.group(1).strip()
    # 提取"目标岗位=XX"或"目标岗位：XX"或"应聘XX岗位"
    jm = re.search(r"(?:目标岗位|应聘岗位|岗位)[=:：]\s*(.+)$", rest)
    if jm:
        return jm.group(1).strip()[:40]
    return rest[:40] if rest else ""


async def optimize_resume(
    target_job: str = "",
    resume_doc: str = RESUME_DOC_DEFAULT,
    user_id: str | None = None,
    request_id: str | None = None,
) -> dict:
    """生成优化简历 → 存 documents → 导出 .docx。"""
    from app.core.memory import normalize_user_id
    from app.services.llm_usage import logical_request_id

    uid = normalize_user_id(user_id)
    logical_id = request_id or logical_request_id("resume_optimize", uid, resume_doc)
    original = _get_resume_text(resume_doc)
    if not original:
        return {"error": f"知识库中未找到简历文档（{resume_doc}），请先通过知识库上传"}

    user_prompt = f"简历原文：\n{original}\n\n"
    user_prompt += f"目标岗位：{target_job}" if target_job else "目标岗位：运维/云运维方向（如未指定）"

    content = await llm.chat(
        [
            {"role": "system", "content": RESUME_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.4,
        max_tokens=4000,
        request_id=logical_id,
        user_id=uid,
    )
    content = content.strip()
    if not content:
        return {"error": "生成失败：LLM 未返回内容"}

    # 标题：目标岗位版简历
    title = "简历优化版" + (f"-{target_job}" if target_job else "")
    result = await documents.generate_and_save(
        title, content, user_id=uid, request_id=f"{logical_id}:document"
    )
    if "error" in result:
        return result

    # 导出 .docx
    try:
        docx_path = documents.export_docx(result["id"])
        result["docx"] = docx_path
    except (ImportError, OSError, ValueError, FileNotFoundError) as e:
        result["docx_error"] = str(e)
    return result
