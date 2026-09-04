"""知识库 API：文档入库 / 检索 / 清单。"""
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.core import knowledge

router = APIRouter()


class IngestRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)  # 文档名
    content: str = Field(..., min_length=1, max_length=1_000_000)  # 全文


class IngestResponse(BaseModel):
    chunks: int
    doc: str
    error: str = ""


@router.post("/knowledge/ingest", response_model=IngestResponse)
async def ingest(req: IngestRequest) -> IngestResponse:
    """上传文档：切块 + 向量化入库。"""
    result = await knowledge.ingest_document(req.name, req.content)
    return IngestResponse(**result)


@router.get("/knowledge/search")
async def search(
    q: str = Query(..., min_length=1, max_length=2_000),
    top_k: int = Query(3, ge=1, le=50),
) -> dict:
    """知识库检索（调试用，聊天已集成）。"""
    hits = await knowledge.search_knowledge(q, top_k=top_k)
    return {"query": q, "hits": hits}


@router.get("/knowledge/docs")
async def docs() -> dict:
    """知识库文档清单。"""
    return {"documents": knowledge.list_documents()}
