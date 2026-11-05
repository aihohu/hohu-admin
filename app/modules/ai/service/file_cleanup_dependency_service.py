"""Context-first retention guard for existing file/batch/task references."""

import json
from dataclasses import dataclass

from sqlalchemy import func, select

from app.core.exceptions import BusinessRuleException
from app.core.tenant import TenantContext
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.atomic_completion_service import FILE_TOOLS
from app.modules.ai.service.completion_read_state_service import (
    CompletionReadStateService,
)
from app.modules.ai.service.operation_log_service import build_target_summary
from app.modules.ai.service.preview_completion_service import verify_historical_preview


@dataclass(frozen=True)
class TransferResource:
    tenant_id: int
    operator_id: int
    kind: str
    resource_id: str
    operation_ids: tuple[str, ...] = ()


def _related(resource, row):
    if row.tenant_id != resource.tenant_id or row.user_id != resource.operator_id:
        return False
    if isinstance(row, AiPreparedAction):
        refs = row.subject_refs
        if str(row.action_id) in resource.operation_ids:
            return True
    else:
        if row.tool_call_id in resource.operation_ids:
            return True
        try:
            refs = json.loads(row.target_summary)
        except (ValueError, TypeError):
            return True  # Unbound legacy file writes cannot be excluded.
    if build_target_summary(refs) is None:
        return True
    return {"type": resource.kind, "id": resource.resource_id} in refs


async def lock_transfer_dependencies(db, resources):
    """Plan all candidates together, then C/source/PA/log; no late inverse lock."""
    if not resources:
        return {}
    resources = tuple(resources)
    tenant_ids = {r.tenant_id for r in resources}

    actions, logs = await scan_transfer_dependencies(db, resources)

    before = transfer_bindings(actions, logs)
    await db.execute(select(func.set_config("lock_timeout", "2s", True)))
    for model, column, ids in (
        (
            AiConversation,
            AiConversation.conversation_id,
            {r.conversation_id for r in [*actions, *logs]},
        ),
        (
            AiMessage,
            AiMessage.message_id,
            {r.source_user_message_id for r in [*actions, *logs]} - {None},
        ),
        (AiPreparedAction, AiPreparedAction.action_id, {a.action_id for a in actions}),
        (AiOperationLog, AiOperationLog.log_id, {log.log_id for log in logs}),
    ):
        if ids:
            await db.execute(
                select(model)
                .where(
                    model.tenant_id.in_(tenant_ids),
                    column.in_(ids),
                )
                .order_by(model.tenant_id, column)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
    actions, logs = await scan_transfer_dependencies(db, resources)
    if transfer_bindings(actions, logs) != before:
        return dict.fromkeys(resources, False)
    return await validate_transfer_dependencies(db, resources, actions, logs)


async def scan_transfer_dependencies(db, resources):
    tenant_ids = {r.tenant_id for r in resources}
    actor_ids = {r.operator_id for r in resources}
    actions = list(
        await db.scalars(
            select(AiPreparedAction)
            .where(
                AiPreparedAction.tenant_id.in_(tenant_ids),
                AiPreparedAction.user_id.in_(actor_ids),
                AiPreparedAction.execute_tool_name.in_(FILE_TOOLS),
            )
            .execution_options(populate_existing=True)
        )
    )
    logs = list(
        await db.scalars(
            select(AiOperationLog)
            .where(
                AiOperationLog.tenant_id.in_(tenant_ids),
                AiOperationLog.user_id.in_(actor_ids),
                AiOperationLog.tool_name.in_(FILE_TOOLS),
            )
            .execution_options(populate_existing=True)
        )
    )
    actions = [a for a in actions if any(_related(r, a) for r in resources)]
    calls = {(a.tenant_id, a.execute_tool_call_id) for a in actions}
    logs = [
        log
        for log in logs
        if (log.tenant_id, log.tool_call_id) in calls
        or any(_related(r, log) for r in resources)
    ]
    return actions, logs


def transfer_bindings(actions, logs):
    return tuple(
        sorted(
            [
                (
                    "pa",
                    a.tenant_id,
                    a.action_id,
                    a.conversation_id,
                    a.source_user_message_id,
                    a.execute_tool_call_id,
                    a.confirmation_id,
                )
                for a in actions
            ]
            + [
                (
                    "log",
                    log.tenant_id,
                    log.log_id,
                    log.conversation_id,
                    log.source_user_message_id,
                    log.tool_call_id,
                    log.confirmation_id,
                )
                for log in logs
            ]
        )
    )


async def validate_transfer_dependencies(db, resources, actions, logs):
    by_call = {(log.tenant_id, log.tool_call_id): log for log in logs}
    proven = set()
    resolved_rows = []
    unresolved = []
    for action in actions:
        log = by_call.get((action.tenant_id, action.execute_tool_call_id))
        tenant = TenantContext(
            tenant_id=action.tenant_id,
            tenant_code="retention",
            actor_user_id=action.user_id,
            tenant_version=1,
            source="access_token",
        )
        state = CompletionReadStateService._project(
            action,
            log,
            await db.get(AiConversation, action.conversation_id),
            await db.get(AiMessage, action.source_user_message_id),
            tenant=tenant,
        )
        if state.unknown:
            unresolved.append(action)
        elif log is not None:
            proven.add((log.tenant_id, log.log_id))
            resolved_rows.append(action)
    for log in logs:
        if "__v0162_reconciliation_v1" in (log.result_summary or ""):
            unresolved.append(log)
        elif (log.tenant_id, log.log_id) not in proven:
            try:
                await verify_historical_preview(
                    db,
                    log,
                    await db.get(AiConversation, log.conversation_id),
                    await db.get(AiMessage, log.source_user_message_id),
                    tenant_id=log.tenant_id,
                    user_id=log.user_id,
                )
                resolved_rows.append(log)
            except (BusinessRuleException, TypeError, ValueError):
                unresolved.append(log)

    def owned_reference_is_proven(resource, operation_id):
        for row in resolved_rows:
            if (
                row.tenant_id != resource.tenant_id
                or row.user_id != resource.operator_id
            ):
                continue
            if isinstance(row, AiPreparedAction):
                matched = str(row.action_id) == operation_id
                refs = row.subject_refs
            else:
                matched = row.tool_call_id == operation_id
                refs = json.loads(row.target_summary)
            if matched and {"type": resource.kind, "id": resource.resource_id} in refs:
                return True
        return False

    return {
        r: not any(_related(r, row) for row in unresolved)
        and all(
            owned_reference_is_proven(r, operation_id)
            for operation_id in r.operation_ids
        )
        for r in resources
    }
