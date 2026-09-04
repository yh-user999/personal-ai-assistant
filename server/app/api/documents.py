"""文档 API：生成保存 / 列表 / 详情。"""
from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services import documents

router = APIRouter()


class GenerateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    requirement: str = Field("", max_length=20_000)


@router.post("/documents/generate")
async def generate(req: GenerateRequest) -> dict:
    """LLM 生成文档并保存（documents 表 + 知识库同步）。"""
    return await documents.generate_and_save(req.title, req.requirement)


@router.get("/documents")
async def list_docs() -> dict:
    return {"documents": documents.list_documents()}


@router.get("/documents/{doc_id}")
async def get_doc(doc_id: int) -> dict:
    doc = documents.get_document(doc_id)
    if not doc:
        return {"error": "not found"}
    return doc
