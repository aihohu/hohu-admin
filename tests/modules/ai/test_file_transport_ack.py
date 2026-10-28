"""FILE3 and preview handoff under actual PostgreSQL protocol truncation."""

import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

from app.core import redis as redis_module
from app.db.session import AsyncSessionLocal
from app.modules.ai.agents.gateway import executor
from app.modules.ai.agents.gateway.atomic_execution import (
    PreparedExecutionOutcomeUnknown,
)
from app.modules.ai.agents.hitl.events import ConfirmationRequiredEvent
from app.modules.ai.api import confirm as confirm_module
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.schemas.confirm import ConfirmRequest
from app.modules.system.models.user import User
from app.modules.system.models.user_transfer import UserExportTask, UserImportBatch
from app.modules.system.service.authorization_ai_tool_adapter import (
    system_ai_tool_adapter,
)
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.ai.test_preview_atomic_completion import authenticated_actor, observe
from tests.modules.ai.test_preview_atomic_completion import preview_case as preview_case
from tests.modules.ai.test_real_commit_ack import (
    acceptance_context,
    commit_status,
    fault_sessions,
)


async def evidence(tmp_path, case, fault, **facts):
    (tmp_path / "file-transport-evidence.json").write_text(
        json.dumps(
            {
                "context": await acceptance_context(case),
                "mode": fault.mode,
                "events": fault.events,
                **facts,
            },
            indent=2,
        ),
        encoding="utf8",
    )


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("boundary", ["preview", "handoff"])
async def test_real_preview_and_handoff_ack(
    preview_case, monkeypatch, tmp_path, committed, boundary
):
    preview = preview_case
    if boundary == "handoff":
        preview.args["requested_outcome"] = "execute_if_approved"
    receipts = []

    async def stop_after_confirmation(event):
        if isinstance(event, ConfirmationRequiredEvent):
            receipts.append(event.confirmation_id)
            raise RuntimeError(
                "Stop the test after observing the original confirmation"
            )

    preview.deps.signal_event = stop_after_confirmation

    def match(session):
        return any(
            (
                boundary == "preview"
                and isinstance(row, AiOperationLog)
                and row.tool_name == "user.import_preview"
                and row.status == "success"
            )
            or (
                boundary == "handoff"
                and isinstance(row, AiPreparedAction)
                and row.execute_tool_name == "user.import_execute"
                and row.status == "pending_confirmation"
            )
            for row in session.identity_map.values()
        )

    async with fault_sessions(
        preview.case,
        monkeypatch,
        [(boundary, "drop_after" if committed else "drop_before")],
        match=match,
    ) as (_, _, faults):
        if committed and boundary == "preview":
            result = await executor.execute_tool(
                "user.import_preview", preview.args, preview.deps
            )
            assert result.ok and result.prepared_action is None
        else:
            with pytest.raises(PreparedExecutionOutcomeUnknown):
                await executor.execute_tool(
                    "user.import_preview", preview.args, preview.deps
                )
        assert faults[0].reached.is_set()
        batches, logs = await observe(preview)
        assert len(preview.calls) == 1
        assert len(batches) == int(committed or boundary == "handoff")
        assert logs[0].status == (
            "success" if committed or boundary == "handoff" else "running"
        )
        assert preview.storage._store
        async with AsyncSessionLocal() as db:
            actions = list(
                await db.scalars(
                    select(AiPreparedAction).where(
                        AiPreparedAction.prepare_tool_call_id == logs[0].tool_call_id,
                        AiPreparedAction.tenant_id == preview.case.tenant.tenant_id,
                    )
                )
            )
        assert len(actions) == int(boundary == "handoff" and committed)
        if actions:
            assert actions[0].status == "pending_confirmation"
            assert receipts == [actions[0].confirmation_id]
        await evidence(
            tmp_path,
            preview.case,
            faults[0],
            boundary=boundary,
            preview_calls=len(preview.calls),
            batch_count=len(batches),
            action_count=len(actions),
            log_status=logs[0].status,
        )


@pytest.mark.parametrize("tool", ["user.export", "user.import_execute"])
@pytest.mark.parametrize("committed", [False, True])
async def test_real_file_completion_ack(
    preview_case, monkeypatch, tmp_path, tool, committed
):
    preview = preview_case
    case = preview.case
    monkeypatch.setattr(confirm_module, "redis_client", redis_module.redis_client)
    monkeypatch.setattr(
        executor.hitl_manager, "hang", AsyncMock(side_effect=TimeoutError)
    )
    monkeypatch.setattr(
        "app.modules.system.service.user_export_service.get_file_storage",
        lambda: preview.storage,
    )
    monkeypatch.setattr(
        "app.modules.system.service.user_import_service.get_file_storage",
        lambda: preview.storage,
    )
    preview.args.update(requested_outcome="execute_if_approved", on_conflict="skip")
    csv = (
        "user_name,dept_input\n"
        f"{case.username},Atomic {case.username.removeprefix('atx')}\n"
        f"{case.username}b,missing-department-for-file-test\n"
    ).encode()
    monkeypatch.setattr(
        "app.modules.system.ai_tools.user_transfer._load_file_bytes",
        AsyncMock(return_value=(csv, "users.csv", "text/csv")),
    )
    handler = system_ai_tool_adapter.apply
    calls, confirmations, export_ids = [], [], []

    async def counted(db, tx, command, work):
        calls.append(True)
        result = await handler(db, tx, command, work)
        assert result.ok
        if tool == "user.export":
            export_ids.append(result.data["exportId"])
        return result

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
    preview.deps.signal_event = approve

    def match(session):
        return any(
            isinstance(row, AiPreparedAction)
            and row.execute_tool_name == tool
            and commit_status(session, row) == "succeeded"
            for row in session.identity_map.values()
        )

    try:
        async with fault_sessions(
            case,
            monkeypatch,
            [(tool, "drop_after" if committed else "drop_before")],
            match=match,
        ) as (_, _, faults):
            entry_tool = (
                "user.export" if tool == "user.export" else "user.import_preview"
            )
            args = (
                {"reason": "real transport acceptance"}
                if tool == "user.export"
                else preview.args
            )
            if committed:
                assert (await executor.execute_tool(entry_tool, args, preview.deps)).ok
            else:
                with pytest.raises(PreparedExecutionOutcomeUnknown):
                    await executor.execute_tool(entry_tool, args, preview.deps)
            assert faults[0].reached.is_set()
            async with AsyncSessionLocal() as db:
                action = await db.scalar(
                    select(AiPreparedAction).where(
                        AiPreparedAction.confirmation_id == confirmations[0]
                    )
                )
                log = await db.scalar(
                    select(AiOperationLog).where(
                        AiOperationLog.tool_call_id == action.execute_tool_call_id
                    )
                )
                if tool == "user.export":
                    rows = list(
                        await db.scalars(
                            select(UserExportTask).where(
                                UserExportTask.export_id.in_(export_ids)
                            )
                        )
                    )
                    assert len(rows) == int(committed)
                else:
                    user = await db.scalar(
                        select(User).where(
                            User.tenant_id == case.tenant.tenant_id,
                            User.user_name == case.username,
                        )
                    )
                    assert (user is not None) == committed
                    batch = await db.get(UserImportBatch, preview.batch_ids[0])
                    assert batch.status.value == (
                        "PARTIAL_SUCCESS" if committed else "PREVIEW_DONE"
                    )
            assert action.status == ("succeeded" if committed else "running")
            assert log.status == ("success" if committed else "running")
            assert len(calls) == 1 and preview.storage._store
            # A second explicit approval only observes the original PA.
            async with AsyncSessionLocal() as db:
                actor = await authenticated_actor(db, tenant=case.tenant)
                response = await confirm_module.confirm_tool(
                    ConfirmRequest(confirmation_id=confirmations[0], action="approve"),
                    db=db,
                    current_user=actor,
                )
                assert response.data.status == action.status
            assert len(calls) == 1
            await evidence(
                tmp_path,
                case,
                faults[0],
                tool=tool,
                handler_calls=len(calls),
                action_status=action.status,
                log_status=log.status,
                artifact_count=len(preview.storage._store),
            )
    finally:
        async with AsyncSessionLocal() as db, db.begin():
            await db.execute(
                delete(UserExportTask).where(UserExportTask.export_id.in_(export_ids))
            )
