"""Recovery entry points and generic writers cannot invent completion facts."""

from datetime import timedelta

import pytest
from sqlalchemy import event, update
from sqlalchemy.orm import Session

from app.core.exceptions import BusinessRuleException
from app.db.session import AsyncSessionLocal
from app.modules.ai.agents.gateway import authorization_execution
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.operation_log_service import operation_log_service
from tests.modules.ai.test_atomic_prepared_completion import (
    approve,
    assert_success,
    count_real_handler,
    observe,
)
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.ai.test_completion_resume_guards import leave_claim_running


async def test_generic_stale_log_writer_cannot_overwrite_atomic_success(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    async with AsyncSessionLocal() as stale:
        cached = await stale.get(AiOperationLog, case.log_id)
        assert cached is not None and cached.status == "pending_confirmation"
        assert (await approve(case)).data.status == "succeeded"
        assert cached.status == "pending_confirmation"
        try:
            await operation_log_service.mark_failed(
                stale,
                case.log_id,
                error_code="LATE_STREAM_FAILURE",
                duration_ms=1,
                tenant=case.tenant,
            )
            await stale.commit()
        except BusinessRuleException:
            await stale.rollback()
    assert_success(case, await observe(case))


@pytest.mark.parametrize("writer", ["success", "failed"])
async def test_generic_log_writer_cannot_terminalize_new_running(
    completion_case, monkeypatch, writer
):
    case = completion_case

    await leave_claim_running(case, monkeypatch)
    async with AsyncSessionLocal() as db:
        try:
            if writer == "success":
                await operation_log_service.mark_success(
                    db,
                    case.log_id,
                    result_summary="late",
                    duration_ms=1,
                    tenant=case.tenant,
                )
            else:
                await operation_log_service.mark_failed(
                    db,
                    case.log_id,
                    error_code="LATE_STREAM_FAILURE",
                    duration_ms=1,
                    tenant=case.tenant,
                )
            await db.commit()
        except BusinessRuleException:
            await db.rollback()
    state = await observe(case)
    assert state["action_status"] == state["operation_status"] == "running"
    assert state["user_ids"] == []


@pytest.mark.parametrize(
    ("stage", "boundary"), [("claim", "after_commit"), ("execution", "before_commit")]
)
async def test_commit_uncertainty_can_fence_proven_database_absence(
    completion_case, monkeypatch, stage, boundary
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    injected = []
    target_status = "running" if stage == "claim" else "succeeded"

    def fail_at_boundary(session):
        if not injected and any(
            isinstance(row, AiPreparedAction)
            and row.action_id == case.action_id
            and row.status == target_status
            for row in session.identity_map.values()
        ):
            injected.append(stage)
            raise RuntimeError("Injected commit-stage uncertainty for recovery")

    if stage == "execution":

        async def fail_execution(_db):
            injected.append(stage)
            raise RuntimeError("Injected execution ACK uncertainty")

        monkeypatch.setattr(
            authorization_execution, "commit_authorization_session", fail_execution
        )
        response = await approve(case)
    else:
        event.listen(Session, boundary, fail_at_boundary)
        try:
            response = await approve(case)
        finally:
            event.remove(Session, boundary, fail_at_boundary)
    assert injected == [stage]
    expected = "running"
    assert response.data.status == expected
    state = await observe(case)
    assert state["action_status"] == state["operation_status"] == expected
    assert state["user_ids"] == []
    assert len(case.calls) == (0 if stage == "claim" else 1)
    assert (await approve(case)).data.status == "running"
    async with AsyncSessionLocal() as db, db.begin():
        await db.execute(
            update(AiPreparedAction)
            .where(AiPreparedAction.action_id == case.action_id)
            .values(
                execution_lease_expires_at=AiPreparedAction.approved_at
                + timedelta(microseconds=1)
            )
        )
    assert (await approve(case)).data.status == "failed"
    assert len(case.calls) == (0 if stage == "claim" else 1)
