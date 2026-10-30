"""Real preview writes observed from a second PostgreSQL connection."""

# ruff: noqa: PLC0415

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, insert, select, update

from app.core import redis as redis_module
from app.core.config import settings
from app.core.file_storage import LocalFileStorage
from app.core.id_generator import next_id
from app.core.security import create_access_token
from app.core.tenant import PlatformContext
from app.db.session import engine
from app.modules.ai.agents.gateway import executor
from app.modules.ai.agents.gateway.atomic_execution import (
    PreparedExecutionOutcomeUnknown,
)
from app.modules.ai.agents.hitl.events import ToolCallResultEvent
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.chat_service import chat_service
from app.modules.auth.service import get_current_user
from app.modules.system.models.file import File
from app.modules.system.models.user import User
from app.modules.system.models.user_transfer import UserImportBatch
from app.modules.system.service.authorization_transfer_candidate import (
    transfer_candidate,
)
from tests.fixture_session import FixtureSessionLocal as AsyncSessionLocal
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case,  # noqa: F401
)


async def finish_legacy_cleanup(intentions):
    """Exercise dormant retention compatibility without replacing runtime owners."""
    from app.modules.ai.agents.gateway import atomic_execution
    from app.modules.system.service.transfer_cleanup_service import (
        finish_committed_cleanup,
    )

    # Only this old helper's private session uses the fixture factory. Scheduled
    # retention and HTTP cancellation keep the production TransferOwner.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(atomic_execution, "AsyncSessionLocal", AsyncSessionLocal)
        await finish_committed_cleanup(intentions)


def _retention_context(reason):
    return PlatformContext(
        actor_principal_id=0,
        actor_name="retention-worker",
        principal_type="service",
        permissions=frozenset({"platform:data:retention"}),
        reason=reason,
        ticket_id="SYSTEM-RETENTION",
        correlation_id=uuid4().hex,
    )


async def authenticated_actor(db, *, tenant):
    version = await db.scalar(
        select(User.auth_version).where(
            User.tenant_id == tenant.tenant_id,
            User.user_id == tenant.actor_user_id,
        )
    )
    return await get_current_user(
        create_access_token(
            subject=str(tenant.actor_user_id),
            tenant_id=tenant.tenant_id,
            tenant_version=1,
            user_version=version,
        ),
        db,
    )


class InspectableLocalStorage(LocalFileStorage):
    @property
    def _store(self):
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("payload")
            if path.is_file()
        }


@pytest.fixture
async def preview_case(completion_case, monkeypatch, tmp_path):  # noqa: F811
    case = completion_case
    # This fault matrix shares the seeded administrator. Keep the real quota
    # machinery, with a test-only limit so >20 cases can reach their fault seam.
    monkeypatch.setattr(
        "app.modules.ai.agents.gateway.quota._resolve_l1_limit",
        AsyncMock(return_value=10000),
    )
    storage = InspectableLocalStorage(tmp_path / "private-artifacts")
    monkeypatch.setattr("app.core.file_storage.get_file_storage", lambda: storage)
    monkeypatch.setattr(executor, "redis_client", redis_module.redis_client)
    csv = (
        "user_name,dept_input\n"
        f"{case.username},Atomic {case.username.removeprefix('atx')}\n"
    ).encode()
    upload = tmp_path / "private-uploads"
    upload.mkdir()
    (upload / "users.csv").write_bytes(csv)
    monkeypatch.setattr(settings, "PRIVATE_UPLOAD_DIR", str(upload))
    file_id = next_id()
    async with AsyncSessionLocal() as setup, setup.begin():
        await setup.execute(
            insert(File).values(
                tenant_id=case.tenant.tenant_id,
                file_id=file_id,
                owner_user_id=case.tenant.actor_user_id,
                original_name="users.csv",
                file_name="users.csv",
                file_path=str(upload / "users.csv"),
                file_url="",
                file_size=len(csv),
                file_ext=".csv",
                mime_type="text/csv",
                business_type="user-import",
                del_flag="0",
            )
        )
    registered = ToolRegistry.get().find("user.import_preview")
    real_handler = transfer_candidate.materialize_preview
    calls = []
    batch_ids = []

    async def counted(db, tx, command, user):
        calls.append(True)
        batch, detail = await real_handler(db, tx, command, user)
        batch_ids.append(batch["batch_id"])
        return batch, detail

    monkeypatch.setattr(transfer_candidate, "materialize_preview", counted)
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(db, tenant=case.tenant)
        action = await db.get(AiPreparedAction, case.action_id)
        deps = await chat_service.build_chat_deps(
            db,
            actor,
            agent_code="user_mgmt",
            trace_id=action.trace_id,
            conversation_id=case.conversation_id,
        )
        deps.conversation_id = case.conversation_id
        deps.source_user_message_id = action.source_user_message_id
        deps.resolved_model_id = action.resolved_model_id
        deps.resolved_provider_id = action.resolved_provider_id
        await db.commit()
        try:
            yield SimpleNamespace(
                case=case,
                deps=deps,
                storage=storage,
                calls=calls,
                batch_ids=batch_ids,
                registered=registered,
                source_path=upload / "users.csv",
                file_id=file_id,
                args={
                    "file_id": str(file_id),
                    "reason": "atomic preview",
                    "requested_outcome": "preview_only",
                },
            )
        finally:
            async with AsyncSessionLocal() as cleanup, cleanup.begin():
                await cleanup.execute(delete(File).where(File.file_id == file_id))
                await cleanup.execute(
                    delete(AiPreparedAction).where(
                        AiPreparedAction.conversation_id == case.conversation_id,
                        AiPreparedAction.action_id != case.action_id,
                    )
                )
                await cleanup.execute(
                    delete(AiOperationLog).where(
                        AiOperationLog.conversation_id == case.conversation_id,
                        AiOperationLog.log_id != case.log_id,
                    )
                )
                await cleanup.execute(
                    delete(UserImportBatch).where(
                        UserImportBatch.tenant_id == case.tenant.tenant_id,
                        UserImportBatch.batch_id.in_(batch_ids),
                    )
                )


async def observe(preview):
    async with AsyncSessionLocal() as db:
        batches = list(
            await db.scalars(
                select(UserImportBatch).where(
                    UserImportBatch.batch_id.in_(preview.batch_ids),
                )
            )
        )
        logs = list(
            await db.scalars(
                select(AiOperationLog).where(
                    AiOperationLog.conversation_id == preview.case.conversation_id,
                    AiOperationLog.tool_name == "user.import_preview",
                )
            )
        )
        return batches, logs


async def replace_source(preview, content):
    preview.source_path.write_bytes(content)
    async with AsyncSessionLocal() as db, db.begin():
        await db.execute(
            update(File)
            .where(File.file_id == preview.file_id)
            .values(file_size=len(content))
        )


async def test_actual_gateway_preview_uses_complete_plan(preview_case):
    writes = []

    def capture(_connection, _cursor, sql, _params, context, _many):
        statement = context.compiled.statement if context.compiled else None
        if (
            sql.startswith("INSERT")
            and getattr(getattr(statement, "table", None), "name", "")
            == "sys_user_import_batch"
        ):
            writes.append(
                context.execution_options.get("_authorization_plan_permit") is not None
            )

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        result = await executor.execute_tool(
            "user.import_preview", preview_case.args, preview_case.deps
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert result.ok
    assert writes == [True]


async def test_completed_preview_reentry_never_reads_or_parses_file(
    preview_case, monkeypatch
):
    from app.core.authorization_io import file_transform_worker
    from app.modules.ai.agents.gateway.preview_execution import execute_preview

    preview = preview_case
    assert (
        await executor.execute_tool("user.import_preview", preview.args, preview.deps)
    ).ok
    batches, logs = await observe(preview)
    forbidden = AsyncMock(
        side_effect=AssertionError("completed preview must not perform file work")
    )
    monkeypatch.setattr(file_transform_worker, "run", forbidden)
    args = {k: v for k, v in preview.args.items() if k != "requested_outcome"}
    result = await execute_preview(
        preview.registered,
        args,
        preview.deps,
        log_id=logs[0].log_id,
        tool_call_id=logs[0].tool_call_id,
        args_hash=logs[0].args_hash,
    )
    assert (
        result.ok and result.prepared_action is None and result.meta["preview_replay"]
    )
    assert result.data["batchId"] == batches[0].batch_id
    forbidden.assert_not_awaited()
    assert len(preview.calls) == 1


@pytest.mark.parametrize("fault", ["version", "scope", "model", "provider"])
async def test_preview_original_authority_is_checked_before_file_work(
    preview_case, monkeypatch, fault
):
    from app.core.authorization_io import file_transform_worker
    from app.modules.ai.agents.gateway.failures import compute_args_hash
    from app.modules.ai.agents.gateway.preview_execution import execute_preview
    from app.modules.ai.agents.hitl.constants import AiExecutionMode

    preview = preview_case
    args = {k: v for k, v in preview.args.items() if k != "requested_outcome"}
    call_id = "tc_" + uuid4().hex
    digest = compute_args_hash(args)
    log_id = await executor._start_log(
        preview.deps,
        preview.registered,
        call_id,
        digest,
        "tool=user.import_preview",
        AiExecutionMode.AUTONOMOUS,
    )
    forbidden = AsyncMock(
        side_effect=AssertionError("stale authority must not perform file work")
    )
    monkeypatch.setattr(file_transform_worker, "run", forbidden)
    version = None
    if fault == "version":
        async with AsyncSessionLocal() as db, db.begin():
            actor = await db.get(User, preview.case.tenant.actor_user_id)
            version = actor.auth_version
            actor.auth_version += 1
    else:
        setattr(
            preview.deps,
            {
                "scope": "data_scope_hash",
                "model": "resolved_model_id",
                "provider": "resolved_provider_id",
            }[fault],
            "0" * 64 if fault == "scope" else 1,
        )
    try:
        with pytest.raises(PreparedExecutionOutcomeUnknown):
            await execute_preview(
                preview.registered,
                args,
                preview.deps,
                log_id=log_id,
                tool_call_id=call_id,
                args_hash=digest,
            )
        forbidden.assert_not_awaited()
        assert preview.calls == []
    finally:
        if version is not None:
            async with AsyncSessionLocal() as db, db.begin():
                await db.execute(
                    update(User)
                    .where(User.user_id == preview.case.tenant.actor_user_id)
                    .values(auth_version=version)
                )


async def test_invalid_proposal_rolls_back_preview_batch(preview_case, monkeypatch):
    from app.modules.ai.service import authorization_preview_candidate

    preview = preview_case
    handler = authorization_preview_candidate._planned_preview_result

    def invalid(*args, **kwargs):
        result = handler(*args, **kwargs)
        result.prepared_action = None
        return result

    monkeypatch.setattr(
        authorization_preview_candidate, "_planned_preview_result", invalid
    )
    try:
        await executor.execute_tool("user.import_preview", preview.args, preview.deps)
    except Exception:
        pass
    batches, logs = await observe(preview)
    assert len(preview.calls) == 1
    assert batches == []
    assert len(logs) == 1 and logs[0].status != "success"
    assert not preview.storage._store  # Invalid proposals fail before publication.


@pytest.mark.parametrize(
    "failure",
    [
        "none",
        "audit",
        "commit_before",
        "commit_after",
        "late_authority",
        "missing_after",
    ],
)
async def test_real_export_file_and_database_completion_faults(
    preview_case, monkeypatch, failure
):

    from app.core.exceptions import AuthorizationException
    from app.modules.ai.agents.gateway import authorization_execution
    from app.modules.ai.agents.hitl.events import ConfirmationRequiredEvent
    from app.modules.ai.api import confirm as confirm_module
    from app.modules.ai.schemas.confirm import ConfirmRequest
    from app.modules.ai.service.authorization_execution_candidate import (
        ai_execution_candidate,
    )
    from app.modules.system.models.user_transfer import UserExportTask
    from app.modules.system.service.authorization_ai_tool_adapter import (
        system_ai_tool_adapter,
    )

    preview = preview_case
    case = preview.case
    monkeypatch.setattr(
        executor.hitl_manager, "hang", AsyncMock(side_effect=TimeoutError)
    )
    monkeypatch.setattr(
        "app.modules.system.service.user_export_service.get_file_storage",
        lambda: preview.storage,
    )
    real_handler = system_ai_tool_adapter.apply
    calls = []
    export_ids = []
    confirmations = []
    real_complete = ai_execution_candidate._complete
    real_commit = authorization_execution.commit_authorization_session
    real_authority = ai_execution_candidate.authorize

    async def counted(db, tx, command, plan):
        calls.append(True)
        result = await real_handler(db, tx, command, plan)
        assert result.ok
        export_ids.append(result.data["exportId"])
        return result

    async def complete(*args, **kwargs):
        await real_complete(*args, **kwargs)
        if failure == "audit":
            raise RuntimeError("injected required file audit rollback")

    async def commit(db):
        if failure.startswith("commit_"):
            if failure == "commit_after":
                await real_commit(db)
            raise RuntimeError("injected export COMMIT acknowledgement failure")
        return await real_commit(db)

    async def authorize(*args, **kwargs):
        if calls and failure == "late_authority":
            raise AuthorizationException(
                "injected late revoke", error_code="AI_TOOL_PERM_DENIED"
            )
        return await real_authority(*args, **kwargs)

    async def approve(event):
        if isinstance(event, ConfirmationRequiredEvent):
            confirmations.append(event.confirmation_id)
            async with AsyncSessionLocal() as db:
                actor = await authenticated_actor(db, tenant=case.tenant)
                await confirm_module.confirm_tool(
                    ConfirmRequest(
                        confirmation_id=event.confirmation_id, action="approve"
                    ),
                    db=db,
                    current_user=actor,
                )

    monkeypatch.setattr(system_ai_tool_adapter, "apply", counted)
    monkeypatch.setattr(ai_execution_candidate, "_complete", complete)
    monkeypatch.setattr(authorization_execution, "commit_authorization_session", commit)
    monkeypatch.setattr(ai_execution_candidate, "authorize", authorize)
    preview.deps.signal_event = approve
    committed = failure in {"none", "commit_after", "missing_after"}
    try:
        if committed:
            result = await executor.execute_tool(
                "user.export", {"reason": "FILE3 regression"}, preview.deps
            )
            assert result.ok
        else:
            with pytest.raises(PreparedExecutionOutcomeUnknown):
                await executor.execute_tool(
                    "user.export", {"reason": "FILE3 regression"}, preview.deps
                )
        async with AsyncSessionLocal() as db:
            action = await db.scalar(
                select(AiPreparedAction).where(
                    AiPreparedAction.confirmation_id == confirmations[0],
                )
            )
            log = await db.scalar(
                select(AiOperationLog).where(
                    AiOperationLog.tool_call_id == action.execute_tool_call_id,
                )
            )
            tasks = list(
                await db.scalars(
                    select(UserExportTask).where(
                        UserExportTask.export_id.in_(export_ids)
                    )
                )
            )
        assert len(calls) == len(export_ids) == len(confirmations) == 1
        assert len(tasks) == int(committed)
        assert action.status == ("succeeded" if committed else "running")
        assert log.status == ("success" if committed else "running")
        assert len(preview.storage._store) == 1
        owner = await preview.storage.artifact_identity(
            next(iter(preview.storage._store))
        )
        assert owner.operation_id == str(action.action_id) and owner.role == "export"
        if failure == "missing_after":
            from app.core.exceptions import BusinessRuleException
            from app.modules.system.service.user_export_service import (
                download_export_file,
            )

            (preview.storage.root / tasks[0].file_storage_key).unlink()
            async with AsyncSessionLocal() as db:
                with pytest.raises(BusinessRuleException, match="核对"):
                    await download_export_file(
                        db,
                        export_ids[0],
                        operator_id=case.tenant.actor_user_id,
                        file_storage=preview.storage,
                        tenant=case.tenant,
                    )
        for _ in range(2):
            async with AsyncSessionLocal() as db:
                actor = await authenticated_actor(db, tenant=case.tenant)
                response = await confirm_module.confirm_tool(
                    ConfirmRequest(confirmation_id=confirmations[0], action="approve"),
                    db=db,
                    current_user=actor,
                )
                assert response.data.status == ("succeeded" if committed else "running")
        assert len(calls) == 1
        if failure == "none":
            from datetime import datetime, timedelta

            from app.modules.system.service.transfer_cleanup_service import (
                take_cleanup_intentions,
            )
            from app.modules.system.service.user_export_service import (
                cleanup_expired_export_tasks,
            )

            async with AsyncSessionLocal() as db, db.begin():
                task = await db.get(UserExportTask, export_ids[0])
                task.created_at = datetime.now() - timedelta(days=31)
            async with AsyncSessionLocal() as db:
                assert (
                    await cleanup_expired_export_tasks(
                        db, platform=_retention_context("export retention proof")
                    )
                    == 1
                )
                intentions = take_cleanup_intentions(db)
                assert preview.storage._store
                await db.commit()
            await finish_legacy_cleanup(intentions)
            assert preview.storage._store == {}
    finally:
        async with AsyncSessionLocal() as db, db.begin():
            await db.execute(
                delete(UserExportTask).where(UserExportTask.export_id.in_(export_ids))
            )


async def test_success_emit_failure_preserves_atomic_audit(preview_case):
    preview = preview_case

    async def fail_receipt(event):
        if isinstance(event, ToolCallResultEvent):
            raise RuntimeError("injected preview receipt failure")

    preview.deps.signal_event = fail_receipt
    with pytest.raises(Exception) as failure:
        await executor.execute_tool("user.import_preview", preview.args, preview.deps)
    batches, logs = await observe(preview)
    assert len(preview.calls) == len(batches) == len(logs) == 1, repr(
        failure.value.__cause__
    )
    assert logs[0].status == "success"
    assert batches[0].batch_id in logs[0].target_summary


async def test_preview_recovery_is_read_only_after_retention(preview_case):
    from app.modules.ai.agents.gateway.preview_execution import read_preview_result
    from app.modules.ai.service.completion_read_state_service import (
        completion_read_state_service,
    )

    preview = preview_case
    result = await executor.execute_tool(
        "user.import_preview", preview.args, preview.deps
    )
    assert result.ok and result.prepared_action is None
    batches, logs = await observe(preview)
    log = logs[0]
    async with AsyncSessionLocal() as db, db.begin():
        await db.execute(
            delete(UserImportBatch).where(
                UserImportBatch.batch_id == batches[0].batch_id
            )
        )
    for _ in range(2):
        replay = await read_preview_result(
            log_id=log.log_id,
            tool_call_id=log.tool_call_id,
            args_hash=log.args_hash,
            deps=preview.deps,
        )
        assert replay.ok and replay.prepared_action is None
        states = await completion_read_state_service.read(
            tenant=preview.case.tenant,
            tool_call_ids=[log.tool_call_id],
        )
        assert (
            states[log.tool_call_id].verified
            and states[log.tool_call_id].status == "success"
        )
    assert len(preview.calls) == 1


async def test_preview_readback_redacts_revoked_projection(preview_case, monkeypatch):
    from app.modules.ai.agents.gateway.preview_execution import read_preview_result
    from app.modules.ai.service.result_projection_service import (
        result_projection_service,
    )

    preview = preview_case
    await executor.execute_tool("user.import_preview", preview.args, preview.deps)
    batches, logs = await observe(preview)
    denied = AsyncMock(return_value=False)
    monkeypatch.setattr(
        result_projection_service, "authorize_result_projection", denied
    )
    replay = await read_preview_result(
        log_id=logs[0].log_id,
        tool_call_id=logs[0].tool_call_id,
        args_hash=logs[0].args_hash,
        deps=preview.deps,
    )
    assert replay.ok and replay.data == {"batchId": batches[0].batch_id}
    assert replay.ui is None and replay.prepared_action is None
    denied.assert_awaited_once()
    assert len(preview.calls) == 1


@pytest.mark.parametrize(
    "field",
    [
        "row_version",
        "tool_codes",
        "subject_refs",
        "subject_refs_hash",
        "projection_dependency_message_ids",
        "resolver_version",
        "approval_mode",
        "dispatch_mode",
        "authorization",
        "expires_at",
        "approved_by",
    ],
)
async def test_handoff_readback_rejects_changed_original_binding(
    preview_case, monkeypatch, field
):
    from copy import deepcopy
    from datetime import timedelta

    from app.modules.ai.agents.gateway.failures import compute_args_hash
    from app.modules.ai.agents.gateway.preview_handoff import read_handoff
    from app.modules.ai.agents.hitl.events import ConfirmationRequiredEvent

    preview = preview_case
    preview.args["requested_outcome"] = "execute_if_approved"
    captured = {}
    original = executor._hang_for_confirmation

    async def capture(*args, **kwargs):
        captured["context"] = kwargs["prepared_action_context"]
        return await original(*args, **kwargs)

    async def stop_receipt(event):
        if isinstance(event, ConfirmationRequiredEvent):
            raise RuntimeError("stop after durable pending")

    monkeypatch.setattr(executor, "_hang_for_confirmation", capture)
    preview.deps.signal_event = stop_receipt
    with pytest.raises(PreparedExecutionOutcomeUnknown):
        await executor.execute_tool("user.import_preview", preview.args, preview.deps)
    context = captured["context"]
    payload = await executor.hitl_manager.get_pending(
        redis_module.redis_client, context.confirmation_id, tenant=preview.case.tenant
    )
    assert payload is not None
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.scalar(
            select(AiPreparedAction).where(
                AiPreparedAction.confirmation_id == context.confirmation_id,
            )
        )
        child = await db.scalar(
            select(AiOperationLog).where(
                AiOperationLog.tool_call_id == context.execute_tool_call_id,
            )
        )
        log_id = child.log_id
        if field == "authorization":
            action.snapshot = deepcopy(action.snapshot)
            action.snapshot["_authorization"]["toolCodes"] = []
            action.snapshot_hash = compute_args_hash(action.snapshot)
        elif field == "expires_at":
            action.expires_at -= timedelta(seconds=1)
        else:
            setattr(
                action,
                field,
                {
                    "row_version": 2,
                    "tool_codes": [],
                    "subject_refs": [],
                    "subject_refs_hash": "0" * 64,
                    "projection_dependency_message_ids": ["9999"],
                    "resolver_version": "changed",
                    "approval_mode": "auto",
                    "dispatch_mode": "queue",
                    "approved_by": preview.case.tenant.actor_user_id,
                }[field],
            )
    with pytest.raises(PreparedExecutionOutcomeUnknown):
        await read_handoff(
            context=context, deps=preview.deps, log_id=log_id, payload=payload
        )
    assert len(preview.calls) == 1


@pytest.mark.parametrize("committed", [False, True])
async def test_preview_commit_uncertainty_never_reexecutes(
    preview_case, monkeypatch, committed
):
    from app.db import authorization_preview_owner

    preview = preview_case
    real_commit = authorization_preview_owner.commit_authorization_session
    attempts = []

    async def fault(db):
        attempts.append(True)
        if committed:
            await real_commit(db)
        raise RuntimeError("injected preview COMMIT acknowledgement failure")

    monkeypatch.setattr(
        authorization_preview_owner, "commit_authorization_session", fault
    )
    if committed:
        result = await executor.execute_tool(
            "user.import_preview", preview.args, preview.deps
        )
        assert result.ok and result.prepared_action is None
    else:
        with pytest.raises(PreparedExecutionOutcomeUnknown):
            await executor.execute_tool(
                "user.import_preview", preview.args, preview.deps
            )
    batches, logs = await observe(preview)
    assert len(preview.calls) == 1
    assert attempts == [True]
    assert len(batches) == int(committed)
    assert logs[0].status == ("success" if committed else "running")
    assert preview.storage._store


@pytest.mark.parametrize("committed", [False, True])
async def test_handoff_commit_uncertainty_keeps_one_original_identity(
    preview_case, monkeypatch, committed
):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.modules.ai.agents.hitl.events import ConfirmationRequiredEvent
    from app.modules.ai.service.prepared_action_service import prepared_action_service

    preview = preview_case
    preview.args["requested_outcome"] = "execute_if_approved"
    real_create = prepared_action_service.create_pending
    real_commit = AsyncSession.commit
    identities = []
    receipts = []

    async def tagged(db, **kwargs):
        action = await real_create(db, **kwargs)
        if action.execute_tool_name == "user.import_execute":
            identities.append((action.confirmation_id, action.execute_tool_call_id))
            db.info["test_handoff_commit"] = True
        return action

    async def fault(db):
        if db.info.pop("test_handoff_commit", False):
            if committed:
                await real_commit(db)
            raise RuntimeError("injected PA COMMIT acknowledgement failure")
        return await real_commit(db)

    async def fail_confirmation(event):
        if isinstance(event, ConfirmationRequiredEvent):
            receipts.append((event.confirmation_id, event.tool_call_id))
            raise RuntimeError("injected confirmation delivery failure")

    monkeypatch.setattr(prepared_action_service, "create_pending", tagged)
    monkeypatch.setattr(AsyncSession, "commit", fault)
    preview.deps.signal_event = fail_confirmation
    with pytest.raises(PreparedExecutionOutcomeUnknown):
        await executor.execute_tool("user.import_preview", preview.args, preview.deps)
    batches, logs = await observe(preview)
    assert len(preview.calls) == len(batches) == len(logs) == len(identities) == 1
    assert logs[0].status == "success"
    async with AsyncSessionLocal() as db:
        actions = list(
            await db.scalars(
                select(AiPreparedAction).where(
                    AiPreparedAction.prepare_tool_call_id == logs[0].tool_call_id,
                )
            )
        )
    assert len(actions) == int(committed)
    if committed:
        assert actions[0].status == "pending_confirmation"
        assert (
            actions[0].confirmation_id,
            actions[0].execute_tool_call_id,
        ) == identities[0]
        assert receipts == identities


async def test_injection_preview_preserves_current_denial_without_file_side_effects(
    preview_case,
    monkeypatch,
):
    from app.modules.ai.agents.hitl.events import ConfirmationRequiredEvent
    from app.modules.ai.api import confirm as confirm_module
    from app.modules.ai.schemas.confirm import ConfirmRequest

    preview = preview_case
    preview.deps.injection_hit = True
    approvals = []

    def forbidden_session():
        pytest.fail("unsupported preview confirmation opened an ordinary session")

    monkeypatch.setattr(executor, "AsyncSessionLocal", forbidden_session)

    async def approve_injection(event):
        if isinstance(event, ConfirmationRequiredEvent):
            async with AsyncSessionLocal() as db:
                actor = await authenticated_actor(db, tenant=preview.case.tenant)
                try:
                    await confirm_module.confirm_tool(
                        ConfirmRequest(
                            confirmation_id=event.confirmation_id, action="approve"
                        ),
                        db=db,
                        current_user=actor,
                    )
                except Exception as exc:
                    approvals.append(getattr(exc, "error_code", None))

    preview.deps.signal_event = approve_injection
    result = await executor.execute_tool(
        "user.import_preview", preview.args, preview.deps
    )
    assert not result.ok
    assert approvals == []  # Existing direct-preview target contract rejects setup.
    assert result.error_code == "AI_CONFIRMATION_SETUP_FAILED"
    assert preview.calls == [] and preview.storage._store == {}


@pytest.mark.parametrize("cancel", [False, True])
async def test_preview_cleanup_keeps_file_until_database_commit(preview_case, cancel):
    from datetime import UTC, datetime, timedelta

    from app.modules.system.service import user_import_service

    preview = preview_case
    await executor.execute_tool("user.import_preview", preview.args, preview.deps)
    batches, _ = await observe(preview)
    key = batches[0].file_storage_key
    async with AsyncSessionLocal() as db, db.begin():
        batch = await db.get(UserImportBatch, batches[0].batch_id)
        batch.created_at = datetime.now(UTC) - timedelta(minutes=11)
    async with AsyncSessionLocal() as db:
        if cancel:
            actor = await authenticated_actor(db, tenant=preview.case.tenant)
            await user_import_service.cancel_batch(
                db,
                batches[0].batch_id,
                actor,
                reason="cancel preview",
                tenant=preview.case.tenant,
                file_storage=preview.storage,
            )
        else:
            assert (
                await user_import_service.cleanup_expired_previews(
                    db,
                    platform=_retention_context("preview fault regression"),
                )
                == 1
            )
        assert key in preview.storage._store
        await db.rollback()
    assert key in preview.storage._store
    current, _ = await observe(preview)
    assert current[0].status.value == "PREVIEW_DONE"


@pytest.mark.parametrize("policy", ["skip", "fail_fast"])
async def test_real_import_partial_and_fail_fast_are_atomic(
    preview_case, monkeypatch, policy
):
    from app.modules.ai.agents.hitl.events import ConfirmationRequiredEvent
    from app.modules.ai.api import confirm as confirm_module
    from app.modules.ai.schemas.confirm import ConfirmRequest
    from app.modules.system.models.user import User

    preview = preview_case
    case = preview.case
    csv = (
        "user_name,dept_input\n"
        f"{case.username},Atomic {case.username.removeprefix('atx')}\n"
        f"{case.username}b,missing-department-for-file-test\n"
    ).encode()
    await replace_source(preview, csv)
    monkeypatch.setattr(
        "app.modules.system.service.user_import_service.get_file_storage",
        lambda: preview.storage,
    )
    preview.args.update(requested_outcome="execute_if_approved", on_conflict=policy)
    confirmations = []
    responses = []

    async def approve(event):
        if isinstance(event, ConfirmationRequiredEvent):
            confirmations.append(event.confirmation_id)
            async with AsyncSessionLocal() as db:
                actor = await authenticated_actor(db, tenant=case.tenant)
                responses.append(
                    await confirm_module.confirm_tool(
                        ConfirmRequest(
                            confirmation_id=event.confirmation_id, action="approve"
                        ),
                        db=db,
                        current_user=actor,
                    )
                )

    preview.deps.signal_event = approve
    result = await executor.execute_tool(
        "user.import_preview", preview.args, preview.deps
    )
    assert result.ok, result.error_code
    assert len(confirmations) == 1 and responses[0].data.status == "succeeded"
    async with AsyncSessionLocal() as db:
        batch = await db.get(UserImportBatch, preview.batch_ids[0])
        user = await db.scalar(
            select(User).where(User.tenant_id == 0, User.user_name == case.username)
        )
        action = await db.scalar(
            select(AiPreparedAction).where(
                AiPreparedAction.confirmation_id == confirmations[0],
            )
        )
        log = await db.scalar(
            select(AiOperationLog).where(
                AiOperationLog.tool_call_id == action.execute_tool_call_id,
            )
        )
    assert action.status == "succeeded" and log.status == "success"
    assert batch.status.value == ("PARTIAL_SUCCESS" if policy == "skip" else "FAILED")
    assert (user is not None) == (policy == "skip")
    identity = await preview.storage.artifact_identity(batch.failed_rows_file)
    assert identity.operation_id == str(action.action_id)
    assert identity.role == "import_errors"
    assert len(preview.calls) == 1
    from datetime import UTC, datetime, timedelta

    from app.modules.system.service.transfer_cleanup_service import (
        take_cleanup_intentions,
    )
    from app.modules.system.service.user_import_service import cleanup_expired_batches

    async with AsyncSessionLocal() as db, db.begin():
        batch = await db.get(UserImportBatch, preview.batch_ids[0])
        batch.created_at = datetime.now(UTC) - timedelta(days=91)
    async with AsyncSessionLocal() as db:
        assert (
            await cleanup_expired_batches(
                db, platform=_retention_context("import retention proof")
            )
            == 1
        )
        intentions = take_cleanup_intentions(db)
        assert len(preview.storage._store) == 2
        await db.commit()
    await finish_legacy_cleanup(intentions)
    assert preview.storage._store == {}


@pytest.mark.parametrize(
    "failure", ["audit", "commit_before", "commit_after", "missing_after"]
)
async def test_real_import_file_failure_preserves_one_execution(
    preview_case, monkeypatch, failure
):
    from app.core.exceptions import BusinessRuleException
    from app.modules.ai.agents.gateway import authorization_execution
    from app.modules.ai.agents.hitl.events import ConfirmationRequiredEvent
    from app.modules.ai.api import confirm as confirm_module
    from app.modules.ai.schemas.confirm import ConfirmRequest
    from app.modules.ai.service.authorization_execution_candidate import (
        ai_execution_candidate,
    )
    from app.modules.system.models.user import User
    from app.modules.system.service.authorization_ai_tool_adapter import (
        system_ai_tool_adapter,
    )

    preview = preview_case
    case = preview.case
    csv = (
        "user_name,dept_input\n"
        f"{case.username},Atomic {case.username.removeprefix('atx')}\n"
        f"{case.username}b,missing-department-for-file-test\n"
    ).encode()
    await replace_source(preview, csv)
    monkeypatch.setattr(
        "app.modules.system.service.user_import_service.get_file_storage",
        lambda: preview.storage,
    )
    monkeypatch.setattr(
        executor.hitl_manager, "hang", AsyncMock(side_effect=TimeoutError)
    )
    preview.args.update(requested_outcome="execute_if_approved", on_conflict="skip")
    handler = system_ai_tool_adapter.apply
    complete = ai_execution_candidate._complete
    commit = authorization_execution.commit_authorization_session
    calls, confirmations = [], []

    async def counted(db, tx, command, plan):
        calls.append(True)
        result = await handler(db, tx, command, plan)
        assert result.ok, result.error_code
        return result

    async def fault_complete(*args, **kwargs):
        await complete(*args, **kwargs)
        if failure == "audit":
            raise RuntimeError("injected import audit rollback")

    async def fault_commit(db):
        if failure.startswith("commit_"):
            if failure == "commit_after":
                await commit(db)
            raise RuntimeError("injected import COMMIT acknowledgement failure")
        return await commit(db)

    async def approve(event):
        if isinstance(event, ConfirmationRequiredEvent):
            confirmations.append(event.confirmation_id)
            async with AsyncSessionLocal() as db:
                actor = await authenticated_actor(db, tenant=case.tenant)
                await confirm_module.confirm_tool(
                    ConfirmRequest(
                        confirmation_id=event.confirmation_id, action="approve"
                    ),
                    db=db,
                    current_user=actor,
                )

    monkeypatch.setattr(system_ai_tool_adapter, "apply", counted)
    monkeypatch.setattr(ai_execution_candidate, "_complete", fault_complete)
    monkeypatch.setattr(
        authorization_execution, "commit_authorization_session", fault_commit
    )
    preview.deps.signal_event = approve
    committed = failure in {"commit_after", "missing_after"}
    if committed:
        result = await executor.execute_tool(
            "user.import_preview", preview.args, preview.deps
        )
        assert result.ok
    else:
        with pytest.raises(PreparedExecutionOutcomeUnknown):
            await executor.execute_tool(
                "user.import_preview", preview.args, preview.deps
            )
    async with AsyncSessionLocal() as db:
        batch = await db.get(UserImportBatch, preview.batch_ids[0])
        user = await db.scalar(
            select(User).where(
                User.tenant_id == case.tenant.tenant_id,
                User.user_name == case.username,
            )
        )
        action = await db.scalar(
            select(AiPreparedAction).where(
                AiPreparedAction.confirmation_id == confirmations[0],
            )
        )
        log = await db.scalar(
            select(AiOperationLog).where(
                AiOperationLog.tool_call_id == action.execute_tool_call_id,
            )
        )
    assert len(calls) == len(preview.calls) == len(confirmations) == 1
    assert (user is not None) == committed
    assert batch.status.value == ("PARTIAL_SUCCESS" if committed else "PREVIEW_DONE")
    assert action.status == ("succeeded" if committed else "running")
    assert log.status == ("success" if committed else "running")
    assert (
        len(preview.storage._store) == 2
    )  # Rollback keeps the private error artifact.
    if failure == "missing_after":
        key = batch.failed_rows_file
        owner = await preview.storage.artifact_identity(key)
        (preview.storage.root / key).unlink()
        with pytest.raises(BusinessRuleException):
            await preview.storage.read_artifact(key, identity=owner)
    for _ in range(2):
        async with AsyncSessionLocal() as db:
            actor = await authenticated_actor(db, tenant=case.tenant)
            response = await confirm_module.confirm_tool(
                ConfirmRequest(confirmation_id=confirmations[0], action="approve"),
                db=db,
                current_user=actor,
            )
            assert response.data.status == ("succeeded" if committed else "running")
    assert len(calls) == len(preview.calls) == 1


@pytest.mark.parametrize("unknown", [False, True, "missing_log"])
async def test_owned_preview_retention_commits_before_delete_and_preserves_unknown(
    preview_case, unknown
):
    from datetime import UTC, datetime, timedelta

    from app.core.exceptions import BusinessRuleException
    from app.modules.system.service.transfer_cleanup_service import (
        take_cleanup_intentions,
    )
    from app.modules.system.service.user_import_service import cleanup_expired_previews

    preview = preview_case
    await executor.execute_tool("user.import_preview", preview.args, preview.deps)
    batches, logs = await observe(preview)
    key = batches[0].file_storage_key
    content = preview.storage._store[key]
    owner = await preview.storage.artifact_identity(key)
    async with AsyncSessionLocal() as db, db.begin():
        batch = await db.get(UserImportBatch, batches[0].batch_id)
        batch.created_at = datetime.now(UTC) - timedelta(minutes=11)
        if unknown:
            log = await db.get(AiOperationLog, logs[0].log_id)
            if unknown == "missing_log":
                await db.delete(log)
            else:
                log.status = "running"
                log.finished_at = None
    async with AsyncSessionLocal() as db:
        count = await cleanup_expired_previews(
            db, platform=_retention_context("owned preview retention")
        )
        assert count == int(not unknown)
        intents = take_cleanup_intentions(db)
        assert key in preview.storage._store
        await db.commit()
    await finish_legacy_cleanup(intents)
    assert (key in preview.storage._store) == bool(unknown)
    if not unknown:
        with pytest.raises(BusinessRuleException):
            await preview.storage.save_artifact(content, identity=owner)
    current, final_logs = await observe(preview)
    assert current[0].status.value == ("PREVIEW_DONE" if unknown else "EXPIRED")
    if unknown == "missing_log":
        assert final_logs == []
    else:
        assert final_logs[0].status == ("running" if unknown else "success")


async def test_expiration_winning_handoff_gap_cannot_revive_import(preview_case):
    import asyncio
    from datetime import UTC, datetime, timedelta

    from app.modules.system.service.transfer_cleanup_service import (
        take_cleanup_intentions,
    )
    from app.modules.system.service.user_import_service import cleanup_expired_previews

    preview = preview_case
    preview.args["requested_outcome"] = "execute_if_approved"
    completed = asyncio.Event()
    release = asyncio.Event()

    async def pause_handoff(event):
        if (
            isinstance(event, ToolCallResultEvent)
            and event.tool == "user.import_preview"
        ):
            completed.set()
            await release.wait()

    preview.deps.signal_event = pause_handoff
    original = asyncio.create_task(
        executor.execute_tool("user.import_preview", preview.args, preview.deps)
    )
    try:
        await asyncio.wait_for(completed.wait(), timeout=5)
        async with AsyncSessionLocal() as db, db.begin():
            batch = await db.get(UserImportBatch, preview.batch_ids[0])
            batch.created_at = datetime.now(UTC) - timedelta(minutes=11)
        async with AsyncSessionLocal() as db:
            assert (
                await cleanup_expired_previews(
                    db, platform=_retention_context("handoff race")
                )
                == 1
            )
            intentions = take_cleanup_intentions(db)
            await db.commit()
        await finish_legacy_cleanup(intentions)
        release.set()
        with pytest.raises(PreparedExecutionOutcomeUnknown):
            await asyncio.wait_for(original, timeout=5)
        batches, logs = await observe(preview)
        assert batches[0].status.value == "EXPIRED" and logs[0].status == "success"
        assert len(preview.calls) == 1
        async with AsyncSessionLocal() as db:
            assert (
                await db.scalar(
                    select(AiPreparedAction.action_id).where(
                        AiPreparedAction.prepare_tool_call_id == logs[0].tool_call_id,
                    )
                )
                is None
            )
    finally:
        release.set()
        if not original.done():
            original.cancel()
            await asyncio.gather(original, return_exceptions=True)


async def test_cleanup_waits_for_preview_context_lock_then_observes_success(
    preview_case,
):
    import asyncio
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text

    from app.modules.ai.models.conversation import AiConversation
    from app.modules.system.service.user_import_service import cleanup_expired_previews

    preview = preview_case
    await executor.execute_tool("user.import_preview", preview.args, preview.deps)
    async with AsyncSessionLocal() as db, db.begin():
        batch = await db.get(UserImportBatch, preview.batch_ids[0])
        batch.created_at = datetime.now(UTC) - timedelta(minutes=11)
    waiter_pid = asyncio.Future()

    async def cleanup():
        async with AsyncSessionLocal() as db:
            waiter_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
            count = await cleanup_expired_previews(
                db, platform=_retention_context("context lock race")
            )
            await db.rollback()
            return count

    async with AsyncSessionLocal() as holder:
        await holder.execute(
            select(AiConversation)
            .where(
                AiConversation.conversation_id == preview.case.conversation_id,
            )
            .with_for_update()
        )
        runner = asyncio.create_task(cleanup())
        try:
            pid = await asyncio.wait_for(waiter_pid, timeout=5)
            async with asyncio.timeout(1), AsyncSessionLocal() as observer:
                while not await observer.scalar(
                    text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}
                ):
                    pass  # Observe the actual PostgreSQL waiter, without a fixed sleep.
            assert not runner.done()
            await holder.rollback()
            assert await asyncio.wait_for(runner, timeout=5) == 1
            assert preview.storage._store
        finally:
            await holder.rollback()
            if not runner.done():
                runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)


async def test_preview_context_lock_has_database_timeout(preview_case):
    import asyncio

    from sqlalchemy.exc import DBAPIError

    from app.modules.ai.models.conversation import AiConversation
    from app.modules.ai.service.preview_completion_service import lock_preview

    preview = preview_case
    await executor.execute_tool("user.import_preview", preview.args, preview.deps)
    _, logs = await observe(preview)
    async with AsyncSessionLocal() as holder:
        await holder.execute(
            select(AiConversation)
            .where(
                AiConversation.conversation_id == preview.case.conversation_id,
            )
            .with_for_update()
        )
        try:
            async with AsyncSessionLocal() as waiter:
                # The server's lock timeout must fire before the outer deadline.
                with pytest.raises(DBAPIError) as failure:
                    async with asyncio.timeout(4):
                        await lock_preview(
                            waiter,
                            log_id=logs[0].log_id,
                            tool_call_id=logs[0].tool_call_id,
                            args_hash=logs[0].args_hash,
                            deps=preview.deps,
                        )
                assert failure.value.orig.sqlstate == "55P03"
                await waiter.rollback()
        finally:
            await holder.rollback()
    assert len(preview.calls) == 1
