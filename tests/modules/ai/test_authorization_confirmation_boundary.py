"""The registered confirmation route never executes an unplanned fallback."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, event, update
from starlette.requests import Request

from app.core.exceptions import BusinessException
from app.db.session import engine
from app.modules.ai.api import confirm, resume
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.schemas.confirm import ConfirmRequest
from tests.fixture_session import FixtureSessionLocal as AsyncSessionLocal
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.ai.test_preview_atomic_completion import authenticated_actor


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.parametrize("durable", [False, True])
async def test_confirm_rejects_missing_or_unadapted_action(
    completion_case, monkeypatch, decision, durable
):
    case = completion_case
    async with AsyncSessionLocal() as db, db.begin():
        if durable:
            await db.execute(
                update(AiPreparedAction)
                .where(AiPreparedAction.action_id == case.action_id)
                .values(execute_tool_name="custom.unadapted")
            )
        else:
            await db.execute(
                delete(AiPreparedAction).where(
                    AiPreparedAction.action_id == case.action_id
                )
            )
    old = AsyncMock(side_effect=AssertionError("legacy executor entered"))
    cache = AsyncMock(side_effect=AssertionError("Redis-only confirmation entered"))
    monkeypatch.setattr(confirm, "_confirm_prepared", old)
    monkeypatch.setattr(confirm.hitl_manager, "get_pending", cache)
    writes = []

    def capture(_conn, _cursor, sql, _params, _context, _many):
        if sql.startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(sql)

    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(db, tenant=case.tenant)
        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        try:
            with pytest.raises(BusinessException) as error:
                await confirm.confirm_tool(
                    ConfirmRequest(
                        confirmation_id=case.confirmation_id, action=decision
                    ),
                    db=db,
                    current_user=actor,
                )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert error.value.error_code == (
        "AI_PREPARED_ACTION_REQUIRED"
        if durable
        else "CONFIRMATION_EXPIRED_OR_NOT_FOUND"
    )
    assert not writes
    old.assert_not_awaited()
    cache.assert_not_awaited()
    assert (
        next(route for route in confirm.router.routes if route.path == "").endpoint
        is confirm.confirm_tool
    )


@pytest.mark.parametrize("durable", [False, True])
async def test_resume_rejects_unadapted_before_cache_or_projection(
    completion_case, monkeypatch, durable
):
    case = completion_case
    async with AsyncSessionLocal() as db, db.begin():
        if durable:
            await db.execute(
                update(AiPreparedAction)
                .where(AiPreparedAction.action_id == case.action_id)
                .values(execute_tool_name="custom.unadapted")
            )
        else:
            await db.execute(
                delete(AiPreparedAction).where(
                    AiPreparedAction.action_id == case.action_id
                )
            )
    cache = AsyncMock(side_effect=AssertionError("unadapted resume read cache"))
    projection = AsyncMock(side_effect=AssertionError("unadapted result projected"))
    monkeypatch.setattr(resume.settings, "AI_SSE_RESUME_ENABLED", True)
    monkeypatch.setattr(resume.hitl_manager, "get_pending", cache)
    monkeypatch.setattr(
        resume.result_projection_service, "authorize_result_projection", projection
    )
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(db, tenant=case.tenant)
        with pytest.raises(BusinessException) as error:
            await resume.resume_chat(
                Request({"type": "http", "headers": []}),
                confirmation_id_query=case.confirmation_id,
                db=db,
                current_user=actor,
            )
    assert error.value.error_code == (
        "AI_PREPARED_ACTION_REQUIRED" if durable else "AI_RESUME_NOT_FOUND"
    )
    cache.assert_not_awaited()
    projection.assert_not_awaited()
