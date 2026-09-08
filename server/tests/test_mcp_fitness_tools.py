"""健身 MCP 工具权限、确认和审计测试。"""
import pytest

from app.mcp.context import McpContext
from app.mcp.permissions import McpPermissionError
from app.mcp.tools import fitness as fitness_tools
from app.models.database import connect


def owner_ctx() -> McpContext:
    return McpContext(uid="owner", role="owner", is_owner=True)


@pytest.mark.asyncio
async def test_fitness_read_tools_are_scoped_and_capped(db):
    result = await fitness_tools.search_fitness_exercises(query="卧推", ctx=owner_ctx())
    assert result["results"]
    summary = await fitness_tools.get_fitness_summary(days=999, ctx=owner_ctx())
    assert summary["days"] == 365


@pytest.mark.asyncio
async def test_fitness_write_requires_confirmation_and_audits(db):
    with pytest.raises(McpPermissionError, match="需要显式确认"):
        await fitness_tools.record_fitness_measurement(
            kind="weight",
            value=70,
            ctx=owner_ctx(),
        )

    result = await fitness_tools.record_fitness_measurement(
        kind="weight",
        value=70,
        confirmed=True,
        ctx=owner_ctx(),
    )
    assert result["recorded"] is True
    row = connect().execute(
        "SELECT tool, success FROM mcp_audit_logs WHERE tool='record_fitness_measurement' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["tool"] == "record_fitness_measurement"
    assert row["success"] == 1


@pytest.mark.asyncio
async def test_fitness_session_tools_flow(db):
    exercise = (await fitness_tools.search_fitness_exercises(query="卧推", ctx=owner_ctx()))["results"][0]
    session = await fitness_tools.start_fitness_session(confirmed=True, ctx=owner_ctx())
    session_id = session["session"]["id"]
    logged = await fitness_tools.log_fitness_set(
        session_id=session_id,
        exercise_id=exercise["id"],
        reps=8,
        weight_kg=60,
        confirmed=True,
        ctx=owner_ctx(),
    )
    assert logged["recorded"] is True
    completed = await fitness_tools.complete_fitness_session(
        session_id=session_id,
        confirmed=True,
        ctx=owner_ctx(),
    )
    assert completed["session"]["status"] == "completed"
