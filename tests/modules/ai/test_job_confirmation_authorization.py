"""Durable cron confirmations retain tenant-scoped job authorization."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tenant_helpers import create_test_tenant, tenant_context

from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.gateway.executor import _build_direct_confirmation_fields
from app.modules.ai.agents.hitl.events import DryRunSummary
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.service.agent_authorization_service import (
    agent_authorization_service,
)
from app.modules.ai.service.prepared_action_service import (
    canonical_payload_hash,
    prepared_action_service,
)
from app.modules.ai.service.result_projection_service import result_projection_service
from app.modules.job import task_registry
from app.modules.job.ai_tools import _dry_run_job_update_cron, job_update_cron
from app.modules.job.models.job import SysJob


@pytest.fixture
async def job_context(db_session, monkeypatch):
    tenant = await create_test_tenant(db_session, prefix="cron-confirm")
    context = tenant_context(tenant_id=tenant.tenant_id, actor_user_id=9001)
    role = SimpleNamespace(
        tenant_id=tenant.tenant_id,
        role_id=11,
        role_code="R_USER",
        status="1",
        data_scope="1",
        depts=[],
        menus=[
            SimpleNamespace(permission="ai:chat:use"),
            SimpleNamespace(permission="system:job:edit"),
        ],
    )
    user = SimpleNamespace(
        tenant_id=tenant.tenant_id,
        user_id=9001,
        roles=[role],
        depts=[],
        _tenant_context=context,
    )
    key = "test_cron_confirmation"
    monkeypatch.setitem(task_registry._task_functions, key, AsyncMock())
    monkeypatch.setitem(task_registry._task_scopes, key, task_registry.TaskScope.TENANT)
    job = SysJob(
        tenant_id=tenant.tenant_id,
        job_name="Nightly test",
        job_key=key,
        cron_expression="0 3 * * *",
        status="2",
    )
    db_session.add(job)
    await db_session.flush()
    # Agent publishing is independently tested; keep real tool permission checks.
    monkeypatch.setattr(
        agent_authorization_service, "authorize_agent_access", AsyncMock()
    )
    registry = ToolRegistry()
    registry.register(job_update_cron.__ai_tool_meta__, job_update_cron)
    monkeypatch.setattr(ToolRegistry, "_instance", registry)
    return SimpleNamespace(db=db_session, user=user, tenant=context), job


async def _allowed(ctx, lineage):
    return await result_projection_service.authorize_result_projection(
        ctx.db, ctx.user, owner_user_id=ctx.user.user_id, lineage=lineage
    )


def _lineage(ctx, job):
    return result_projection_service.freeze_lineage(
        tenant=ctx.tenant,
        agent_code="job_mgmt",
        tool_codes=["job.update_cron"],
        subject_refs=[{"type": "job", "id": str(job.job_id)}],
    )


async def test_cron_preview_persists_authorized_action_and_projects_result(job_context):
    ctx, job = job_context
    preview = await _dry_run_job_update_cron(
        ctx, job_id=job.job_id, cron_expression="0 4 * * *"
    )
    fields = _build_direct_confirmation_fields(
        job_update_cron.__ai_tool_meta__,
        preview.execution_args,
        DryRunSummary(
            summary=preview.reason,
            affected_count=preview.count,
            confirmation_fields=preview.confirmation_fields,
        ),
    )
    action = await prepared_action_service.create_pending(
        ctx.db,
        confirmation_id="cid_test_job_cron",
        prepare_tool_call_id=None,
        prepare_tool_name=None,
        execute_tool_call_id="tc_test_job_cron",
        execute_tool_name="job.update_cron",
        frozen_args=preview.execution_args,
        snapshot={
            "tool": "job.update_cron",
            "argsHash": canonical_payload_hash(preview.execution_args),
            "dryRun": {"summary": preview.reason},
        },
        snapshot_hash="",
        subject_ref=None,
        presentation={"title": "Update cron", "fields": fields},
        interaction_flow="direct",
        requested_outcome="direct",
        user_id=ctx.user.user_id,
        tenant=ctx.tenant,
        conversation_id=100,
        source_user_message_id=101,
        trace_id="tr_test_job_cron",
        agent_code="job_mgmt",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        resolved_model_id=501,
        resolved_provider_id=601,
    )
    await ctx.db.refresh(action)
    assert action.status == "pending_confirmation"
    assert action.subject_refs == [{"type": "job", "id": str(job.job_id)}]
    assert job.cron_expression == "0 3 * * *"
    lineage = result_projection_service.lineage_from_record(action)
    assert await _allowed(ctx, lineage)

    result = await job_update_cron(ctx, **action.frozen_args)
    assert result.ok
    assert result.projection.subject_refs == tuple(action.subject_refs)
    assert job.cron_expression == "0 4 * * *"
    assert job.status == "2"
    assert result.ui.audit["before"] == "0 3 * * *"
    assert result.ui.audit["after"] == "0 4 * * *"
    assert await _allowed(ctx, lineage)
    ctx.user.roles[0].menus.pop()
    assert not await _allowed(ctx, lineage)


@pytest.mark.parametrize("boundary", ["tenant", "platform", "unregistered", "deleted"])
async def test_job_projection_rechecks_current_task_boundary(job_context, boundary):
    ctx, job = job_context
    lineage = _lineage(ctx, job)
    assert await _allowed(ctx, lineage)
    if boundary == "tenant":
        other = await create_test_tenant(ctx.db, prefix="cron-other")
        job.tenant_id = other.tenant_id
    elif boundary == "platform":
        # The fixture's monkeypatch restores the registry after the test.
        task_registry._task_scopes[job.job_key] = task_registry.TaskScope.PLATFORM
    elif boundary == "unregistered":
        job.job_key = "not_registered_cron_confirmation"
    else:
        await ctx.db.delete(job)
    await ctx.db.flush()
    assert not await _allowed(ctx, lineage)


@pytest.mark.parametrize("job_id", [None, True, 0, -1, 1.5, "01", "bad"])
def test_cron_confirmation_rejects_invalid_target(job_id):
    with pytest.raises(BusinessRuleException) as error:
        prepared_action_service._build_subject_refs(
            execute_tool_name="job.update_cron",
            frozen_args={"job_id": job_id},
            snapshot={},
            subject_ref=None,
            projection_kind=None,
        )
    assert error.value.error_code == "AI_PREPARED_ACTION_BINDING_INVALID"


def test_unknown_job_write_still_has_no_confirmation_contract():
    with pytest.raises(BusinessRuleException) as error:
        prepared_action_service._build_subject_refs(
            execute_tool_name="job.unknown_write",
            frozen_args={"job_id": 123},
            snapshot={},
            subject_ref=None,
            projection_kind=None,
        )
    assert error.value.error_code == "AI_PREPARED_ACTION_BINDING_INVALID"
