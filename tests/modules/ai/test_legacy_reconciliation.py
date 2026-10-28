"""Maintenance annotations preserve facts and never grant execution authority."""

# ruff: noqa: F811

import json
from copy import deepcopy
from dataclasses import replace

import pytest
from sqlalchemy import update

from app.core.exceptions import BusinessException
from app.db.session import AsyncSessionLocal
from app.modules.ai.agents.hitl.constants import PreparedActionStatus
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.completion_read_state_service import (
    completion_read_state_service,
)
from app.modules.ai.service.conversation_service import conversation_service
from app.modules.ai.service.legacy_reconciliation_service import (
    legacy_reconciliation_service as service,
)
from app.modules.ai.service.operation_log_service import operation_log_service
from app.modules.ai.service.prepared_action_service import prepared_action_service
from app.modules.ai.service.reconciliation_annotation import MARKER, read_annotation
from tests.modules.ai.test_atomic_prepared_completion import (
    approve,
    authenticated_actor,
    completion_case,  # noqa: F401
)


@pytest.mark.parametrize(
    "status",
    ["pending_confirmation", "running", "failed", "succeeded", "rejected", "expired"],
)
async def test_damaged_annotation_blocks_approve_reject_and_terminal_cas(
    completion_case, status
):
    case = completion_case
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, case.action_id)
        log = await db.get(AiOperationLog, case.log_id)
        action.status = status
        log.result_summary = '{"__v0162_reconciliation_v1": damaged'
    for decision in ("approve", "reject"):
        response = await approve(case, decision=decision)
        assert response.data.status == "running"
    async with AsyncSessionLocal() as db:
        if status == "pending_confirmation":
            assert (
                await prepared_action_service.transition_status(
                    db,
                    action_id=case.action_id,
                    expected_status=PreparedActionStatus.PENDING_CONFIRMATION,
                    expected_version=1,
                    target_status=PreparedActionStatus.EXPIRED,
                    tenant=case.tenant,
                )
                is None
            )
        await db.commit()
    async with AsyncSessionLocal() as db:
        assert (await db.get(AiPreparedAction, case.action_id)).status == status
        assert (
            await db.get(AiOperationLog, case.log_id)
        ).status == "pending_confirmation"


async def test_annotation_reloads_retained_orm_facts_after_locking(completion_case):
    case = completion_case
    async with AsyncSessionLocal() as setup, setup.begin():
        log = await setup.get(AiOperationLog, case.log_id)
        log.status = "running"
    async with AsyncSessionLocal() as db:
        retained = await db.get(AiPreparedAction, case.action_id)
        baseline = await service.inspect(db, tenant=case.tenant, log_id=case.log_id)
        async with AsyncSessionLocal() as writer, writer.begin():
            await writer.execute(
                update(AiPreparedAction)
                .where(AiPreparedAction.action_id == case.action_id)
                .values(result_data={"concurrent": True})
            )
        assert retained.result_data is None
        with pytest.raises(BusinessException):
            await service.annotate(
                db,
                tenant=case.tenant,
                log_id=case.log_id,
                baseline=baseline["baseline"],
                reason="LEGACY_RUNNING",
                evidence_sha256="a" * 64,
                maintenance_sha256="b" * 64,
            )
        await db.rollback()


async def test_missing_and_wrong_source_are_inventory_only(completion_case):
    case = completion_case
    async with AsyncSessionLocal() as db, db.begin():
        assert await service.inspect(db, tenant=case.tenant, log_id=1) == {
            "state": "missing"
        }
        log = await db.get(AiOperationLog, case.log_id)
        log.status = "running"
        log.source_user_message_id = None
        await db.flush()
        inventory = await service.inspect(db, tenant=case.tenant, log_id=case.log_id)
        assert inventory["state"] == "quarantined"
        with pytest.raises(BusinessException):
            await service.annotate(
                db,
                tenant=case.tenant,
                log_id=case.log_id,
                baseline=inventory["baseline"],
                reason="LEGACY_RUNNING",
                evidence_sha256="a" * 64,
                maintenance_sha256="b" * 64,
            )
        assert log.result_summary is None


async def test_annotation_hides_failure_and_pending_without_changing_facts(
    completion_case,
):
    case = completion_case
    async with AsyncSessionLocal() as db, db.begin():
        log = await db.get(AiOperationLog, case.log_id)
        log.status = "running"
        await db.flush()
        before = await service.inspect(db, tenant=case.tenant, log_id=case.log_id)
        await service.annotate(
            db,
            tenant=case.tenant,
            log_id=case.log_id,
            baseline=before["baseline"],
            reason="LEGACY_RUNNING",
            evidence_sha256="a" * 64,
            maintenance_sha256="b" * 64,
        )
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(case, db)
        messages = await conversation_service.project_messages(
            db, conversation_id=case.conversation_id, current_user=actor
        )
        assistant = next(item for item in messages if item.role == "assistant")
        assert (
            "请勿重新办理" in assistant.content
            and case.tool_call_id in assistant.content
        )
        assert MARKER not in assistant.model_dump_json()
        assert (
            "ok" not in assistant.tool_calls[0]
            and "error_code" not in assistant.tool_calls[0]
        )
        assert not await prepared_action_service.list_pending_for_conversation(
            db,
            conversation_id=case.conversation_id,
            user_id=actor.user_id,
            tenant=case.tenant,
        )
        assert await prepared_action_service.has_in_progress_for_conversation(
            db,
            conversation_id=case.conversation_id,
            user_id=actor.user_id,
            tenant=case.tenant,
        )
        with pytest.raises(BusinessException):
            await operation_log_service.mark_success(
                db,
                case.log_id,
                tenant=case.tenant,
                result_summary="late success",
                duration_ms=1,
            )
        await db.rollback()


async def test_append_and_corruption_never_grant_authority(completion_case):
    case = completion_case
    async with AsyncSessionLocal() as db, db.begin():
        log = await db.get(AiOperationLog, case.log_id)
        log.status = "running"
        await db.flush()
        inventory = await service.inspect(db, tenant=case.tenant, log_id=case.log_id)
        await service.annotate(
            db,
            tenant=case.tenant,
            log_id=case.log_id,
            baseline=inventory["baseline"],
            reason="LEGACY_RUNNING",
            evidence_sha256="a" * 64,
            maintenance_sha256="b" * 64,
        )
        inventory = await service.inspect(db, tenant=case.tenant, log_id=case.log_id)
        await service.annotate(
            db,
            tenant=case.tenant,
            log_id=case.log_id,
            baseline=inventory["baseline"],
            reason="LEGACY_UNVERIFIED",
            evidence_sha256="c" * 64,
            maintenance_sha256="b" * 64,
        )
        root = json.loads(log.result_summary)
        assert len(root[MARKER]["history"]) == 2
        for kind in ("disposition", "extra", "binding", "duplicate", "reason"):
            broken = deepcopy(root)
            if kind == "disposition":
                broken[MARKER]["disposition"] = "verified"
            if kind == "extra":
                broken["extra"] = True
            if kind == "binding":
                broken[MARKER]["actionId"] = "garbage"
            if kind == "reason":
                broken[MARKER]["reasonCode"] = "FORCE_SUCCESS"
            raw = json.dumps(broken)
            if kind == "duplicate":
                raw = raw.replace(
                    '"previousSummary": null',
                    '"previousSummary": null, "previousSummary": "x"',
                )
            with pytest.raises(BusinessException):
                read_annotation(raw)
        log.error_code = "changed"
        await db.flush()
        assert (await service.inspect(db, tenant=case.tenant, log_id=case.log_id))[
            "state"
        ] == "quarantined"
    async with AsyncSessionLocal() as db:
        with pytest.raises(BusinessException):
            await service.inspect(
                db, tenant=replace(case.tenant, tenant_id=999999), log_id=case.log_id
            )


@pytest.mark.parametrize("original", [None, "", "legacy failure summary"])
@pytest.mark.parametrize(
    "error_code",
    [
        "AI_INTERNAL_ERROR",
        "AI_PREPARED_ACTION_EXECUTION_INTERRUPTED",
        "AI_HITL_EXECUTION_INTERRUPTED",
    ],
)
async def test_annotation_preserves_facts_and_is_idempotent(
    completion_case, original, error_code
):
    case = completion_case
    async with AsyncSessionLocal() as db, db.begin():
        await db.execute(
            update(AiOperationLog)
            .where(AiOperationLog.log_id == case.log_id)
            .values(status="failed", error_code=error_code, result_summary=original)
        )
    async with AsyncSessionLocal() as db, db.begin():
        before = await service.inspect(db, tenant=case.tenant, log_id=case.log_id)
        result = await service.annotate(
            db,
            tenant=case.tenant,
            log_id=case.log_id,
            baseline=before["baseline"],
            reason="LEGACY_INTERRUPTED",
            evidence_sha256="a" * 64,
            maintenance_sha256="b" * 64,
        )
        log = await db.get(AiOperationLog, case.log_id)
        saved = log.result_summary
        envelope = json.loads(saved)
        assert set(envelope) == {"__v0162_reconciliation_v1", "previousSummary"}
        assert envelope["previousSummary"] == original
        assert log.status == "failed" and log.error_code == error_code
        assert result["disposition"] == "unverified"
        again = await service.annotate(
            db,
            tenant=case.tenant,
            log_id=case.log_id,
            baseline=before["baseline"],
            reason="LEGACY_INTERRUPTED",
            evidence_sha256="a" * 64,
            maintenance_sha256="b" * 64,
        )
        assert again == result and log.result_summary == saved
    states = await completion_read_state_service.read(
        tenant=case.tenant, tool_call_ids=[case.tool_call_id]
    )
    assert states[case.tool_call_id].unknown
    assert states[case.tool_call_id].finished_at is None


async def test_changed_baseline_and_malformed_annotation_are_quarantined(
    completion_case,
):
    case = completion_case
    async with AsyncSessionLocal() as db, db.begin():
        log = await db.get(AiOperationLog, case.log_id)
        log.status = "running"
        await db.flush()
        before = await service.inspect(db, tenant=case.tenant, log_id=case.log_id)
        log.result_summary = "changed"
        await db.flush()
        with pytest.raises(BusinessException):
            await service.annotate(
                db,
                tenant=case.tenant,
                log_id=case.log_id,
                baseline=before["baseline"],
                reason="LEGACY_RUNNING",
                evidence_sha256="a" * 64,
                maintenance_sha256="b" * 64,
            )
        log.result_summary = '{"__v0162_reconciliation_v1":'
        await db.flush()
        blocked = await service.inspect(db, tenant=case.tenant, log_id=case.log_id)
        assert blocked["state"] == "quarantined"
        with pytest.raises(BusinessException):
            await service.annotate(
                db,
                tenant=case.tenant,
                log_id=case.log_id,
                baseline=blocked["baseline"],
                reason="LEGACY_RUNNING",
                evidence_sha256="a" * 64,
                maintenance_sha256="b" * 64,
            )
