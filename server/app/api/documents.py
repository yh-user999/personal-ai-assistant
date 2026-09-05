"""文档 API：生成保存 / 列表 / 详情。"""
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.auth import require_roles
from app.core.memory import owner_user_id
from app.services import documents

router = APIRouter()


def _subject(request: Request) -> str:
    auth = require_roles(request, "owner", "internal")
    return str(auth.subject or owner_user_id())


class GenerateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    requirement: str = Field("", max_length=20_000)


@router.post("/documents/generate")
async def generate(req: GenerateRequest, request: Request) -> dict:
    """LLM 生成文档并保存（documents 表 + 知识库同步）。"""
    uid = _subject(request)
    return await documents.generate_and_save(
        req.title,
        req.requirement,
        user_id=uid,
        request_id=request.headers.get("x-request-id") or None,
    )


@router.get("/documents")
async def list_docs(request: Request) -> dict:
    _subject(request)
    return {"documents": documents.list_documents()}


@router.get("/documents/{doc_id}")
async def get_doc(doc_id: int, request: Request) -> dict:
    _subject(request)
    doc = documents.get_document(doc_id)
    if not doc:
        return {"error": "not found"}
    return doc
