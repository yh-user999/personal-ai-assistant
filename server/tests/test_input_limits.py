"""API 输入边界与请求级状态隔离测试。"""
import asyncio

import pytest
from pydantic import ValidationError

from app.api.documents import GenerateRequest
from app.api.events import BehaviorEvent, EventBatch, HeartbeatBody
from app.api.knowledge import IngestRequest
from app.core import knowledge, memory


def test_knowledge_models_bound_text():
    with pytest.raises(ValidationError):
        IngestRequest(name="", content="x")
    with pytest.raises(ValidationError):
        IngestRequest(name="x", content="a" * 1_000_001)


def test_knowledge_search_bounds_are_declared():
    from app.main import app

    schema = app.openapi()["paths"]["/api/knowledge/search"]["get"]["parameters"]
    top_k = next(p for p in schema if p["name"] == "top_k")["schema"]
    assert top_k["minimum"] == 1
    assert top_k["maximum"] == 50
    with pytest.raises(ValidationError):
        IngestRequest(name="x" * 201, content="x")


def test_event_batch_and_document_bounds():
    assert EventBatch(events=[]).events == []
    with pytest.raises(ValidationError):
        EventBatch(events=[{"kind": "x", "name": "n"}] * 101)
    with pytest.raises(ValidationError):
        BehaviorEvent(kind="x", name="n", detail="x" * 501)
    with pytest.raises(ValidationError):
        BehaviorEvent(kind="x", name="n", meta={str(i): i for i in range(51)})
    with pytest.raises(ValidationError):
        GenerateRequest(title="", requirement="")
    with pytest.raises(ValidationError):
        GenerateRequest(title="ok", requirement="x" * 20_001)


def test_request_state_is_task_local():
    async def read_cached_value():
        return memory.take_query_vec("same")

    async def set_and_read():
        memory._last_query_vec.set(("same", [1.0]))
        await asyncio.sleep(0)
        return memory.take_query_vec("same")

    async def run():
        memory._last_query_vec.set(None)
        first, second = await asyncio.gather(set_and_read(), read_cached_value())
        return first, second

    assert asyncio.run(run()) == ([1.0], None)


def test_vector_degraded_flag_is_task_local():
    async def set_flag():
        knowledge._vector_degraded_last.set(True)
        await asyncio.sleep(0)
        return knowledge.last_vector_degraded()

    async def read_flag():
        await asyncio.sleep(0)
        return knowledge.last_vector_degraded()

    async def run():
        knowledge._vector_degraded_last.set(False)
        return await asyncio.gather(set_flag(), read_flag())

    assert asyncio.run(run()) == [True, False]
