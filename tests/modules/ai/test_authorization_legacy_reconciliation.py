"""Offline legacy annotations require real plan permits and an issued fence."""

import pytest
from sqlalchemy import event, select, update

from app.core.exceptions import BusinessException
from app.db.authorization_http import get_authorization_runtime
from app.db.session import engine
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.reconciliation_annotation import MARKER, read_annotation
from tests.fixture_session import FixtureSessionLocal
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.platform.test_authorization_principal_candidate import fence


@pytest.fixture
async def legacy(completion_case):
    case = completion_case
    async with FixtureSessionLocal() as db, db.begin():
        await db.execute(
            update(AiOperationLog)
            .where(AiOperationLog.log_id == case.log_id)
            .values(status="running")
        )
    owner = get_authorization_runtime().reconciliation
    request = {"tenant_id": 0, "operator_id": case.tenant.actor_user_id}
    manifest = await owner.inspect(**request, log_ids=(case.log_id,))
    return case, owner, request, manifest


async def test_annotation_requires_stop_evidence_before_database(legacy):
    _, owner, request, manifest = legacy
    with pytest.raises(BusinessException) as error:
        await owner.annotate(
            **request,
            manifest=manifest,
            reason="LEGACY_RUNNING",
            evidence_sha256="a" * 64,
            maintenance_sha256="b" * 64,
            fence=None,
        )
    assert error.value.error_code == "AUTHORIZATION_MAINTENANCE_FENCE_REQUIRED"


async def test_planned_annotation_preserves_history_and_replays_noop(legacy):
    case, owner, request, manifest = legacy
    permit = await fence("ai.reconcile")
    writes = []

    def capture(_conn, _cursor, sql, _params, context, _many):
        if sql.startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(context.execution_options.get("_authorization_plan_permit"))

    args = dict(
        **request,
        manifest=manifest,
        reason="LEGACY_RUNNING",
        evidence_sha256="a" * 64,
        maintenance_sha256=permit.evidence.manifest_digest,
        fence=permit,
    )
    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        result = await owner.annotate(**args)
        assert await owner.annotate(**args) == result
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert len(writes) == 2 and all(writes)
    async with FixtureSessionLocal() as db:
        log = await db.get(AiOperationLog, case.log_id)
        action = await db.get(AiPreparedAction, case.action_id)
        assert log.status == "running" and log.finished_at is None
        assert action.status == "pending_confirmation"
        assert len(read_annotation(log.result_summary)[MARKER]["history"]) == 1
        message = await db.scalar(
            select(AiMessage).where(
                AiMessage.conversation_id == case.conversation_id,
                AiMessage.role == "assistant",
            )
        )
        assert "请勿重新办理" in message.content


async def test_stale_inventory_and_forged_maintenance_digest_cannot_annotate(legacy):
    case, owner, request, manifest = legacy
    permit = await fence("ai.reconcile")
    args = dict(
        **request,
        manifest=manifest,
        reason="LEGACY_RUNNING",
        evidence_sha256="a" * 64,
        maintenance_sha256="f" * 64,
        fence=permit,
    )
    with pytest.raises(BusinessException):
        await owner.annotate(**args)
    args["maintenance_sha256"] = permit.evidence.manifest_digest
    async with FixtureSessionLocal() as db, db.begin():
        await db.execute(
            update(AiPreparedAction)
            .where(AiPreparedAction.action_id == case.action_id)
            .values(result_data={"changed": True})
        )
    with pytest.raises(BusinessException):
        await owner.annotate(**args)
    async with FixtureSessionLocal() as db:
        assert (await db.get(AiOperationLog, case.log_id)).result_summary is None
