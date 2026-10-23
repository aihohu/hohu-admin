"""Context-first dependency plans for User FK cascades.

The caller owns rollback/retry. This module never locks User rows: the system
service takes its domain locks and asks us to recheck the complete dependency
set before deleting. New conversations are fenced by that final User FOR UPDATE
and their real FK; existing-context writers must take the conversation lock.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleException
from app.core.tenant import TenantContext
from app.modules.ai.agents.gateway.failures import compute_args_hash
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.atomic_completion_service import (
    atomic_completion_service,
    is_registered_readonly_action,
)

if TYPE_CHECKING:
    from app.modules.ai.service.atomic_completion_service import ClaimBinding

_PLAN_KEY = "v0162_user_delete_locked_plan"


class UserDeleteDependencyChanged(BusinessRuleException):
    """Release the entire transaction before rebuilding earlier context locks."""

    def __init__(self) -> None:
        super().__init__(
            "用户关联操作已变化，请重新核对后删除",
            error_code="USER_DELETE_DEPENDENCY_CHANGED",
        )
        self.code = 409


def _unresolved() -> BusinessRuleException:
    error = BusinessRuleException(
        "用户仍有关联 AI 操作待完成或核对，暂不能删除",
        error_code="USER_AI_DEPENDENCY_UNRESOLVED",
    )
    error.code = 409
    return error


@dataclass(frozen=True, slots=True)
class UserDeleteDependencyPlan:
    tenant_id: int
    target_user_ids: tuple[int, ...]
    conversation_ids: tuple[int, ...]
    source_ids: tuple[int, ...]
    action_ids: tuple[int, ...]
    log_ids: tuple[int, ...]
    target_action_ids: tuple[int, ...]
    target_log_ids: tuple[int, ...]
    bindings: tuple[tuple, ...]
    own_claim: "ClaimBinding | None" = None


class UserDeleteDependencyService:
    async def plan(
        self,
        db: AsyncSession,
        *,
        target_user_ids: Sequence[int],
        tenant: TenantContext,
        own_claim: "ClaimBinding | None" = None,
    ) -> UserDeleteDependencyPlan:
        """Discover immutable routing facts without taking any row lock."""
        isolation = await db.scalar(
            select(func.current_setting("transaction_isolation"))
        )
        if isolation != "read committed":
            raise BusinessRuleException(
                "用户删除需要 READ COMMITTED 事务隔离级别",
                error_code="USER_DELETE_ISOLATION_UNSUPPORTED",
            )
        targets = tuple(sorted(set(target_user_ids)))
        if not targets or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in targets
        ):
            raise UserDeleteDependencyChanged()
        if own_claim is not None and (
            own_claim.tenant_id != tenant.tenant_id
            or own_claim.actor_user_id != tenant.actor_user_id
            or own_claim.execute_tool_name != "user.batch_delete"
            or own_claim.actor_user_id in targets
        ):
            raise UserDeleteDependencyChanged()
        conversations = list(
            (
                await db.execute(
                    select(
                        AiConversation.conversation_id, AiConversation.user_id
                    ).where(
                        AiConversation.tenant_id == tenant.tenant_id,
                        AiConversation.user_id.in_(targets),
                    )
                )
            ).all()
        )
        conversation_ids = {row.conversation_id for row in conversations}
        messages = list(
            (
                await db.execute(
                    select(
                        AiMessage.message_id,
                        AiMessage.conversation_id,
                        AiMessage.role,
                        AiMessage.trace_id,
                    ).where(
                        AiMessage.tenant_id == tenant.tenant_id,
                        AiMessage.conversation_id.in_(conversation_ids),
                    )
                )
            ).all()
        )
        source_ids = {row.message_id for row in messages}
        actions = list(
            (
                await db.execute(
                    select(
                        AiPreparedAction.action_id,
                        AiPreparedAction.user_id,
                        AiPreparedAction.conversation_id,
                        AiPreparedAction.source_user_message_id,
                        AiPreparedAction.execute_tool_call_id,
                        AiPreparedAction.confirmation_id,
                    ).where(
                        AiPreparedAction.tenant_id == tenant.tenant_id,
                        or_(
                            AiPreparedAction.user_id.in_(targets),
                            AiPreparedAction.conversation_id.in_(conversation_ids),
                            AiPreparedAction.source_user_message_id.in_(source_ids),
                        ),
                    )
                )
            ).all()
        )
        logs = list(
            (
                await db.execute(
                    select(
                        AiOperationLog.log_id,
                        AiOperationLog.user_id,
                        AiOperationLog.conversation_id,
                        AiOperationLog.source_user_message_id,
                        AiOperationLog.tool_call_id,
                        AiOperationLog.confirmation_id,
                    ).where(
                        AiOperationLog.tenant_id == tenant.tenant_id,
                        or_(
                            AiOperationLog.user_id.in_(targets),
                            AiOperationLog.conversation_id.in_(conversation_ids),
                            AiOperationLog.source_user_message_id.in_(source_ids),
                            AiOperationLog.tool_call_id.in_(
                                row.execute_tool_call_id for row in actions
                            ),
                            AiOperationLog.confirmation_id.in_(
                                row.confirmation_id for row in actions
                            ),
                        ),
                    )
                )
            ).all()
        )
        target_action_ids = {row.action_id for row in actions}
        target_log_ids = {row.log_id for row in logs}
        for row in [*actions, *logs]:
            conversation_ids.add(row.conversation_id)
            if row.source_user_message_id is not None:
                source_ids.add(row.source_user_message_id)
        action_ids = set(target_action_ids)
        log_ids = set(target_log_ids)
        if own_claim is not None:
            conversation_ids.add(own_claim.conversation_id)
            source_ids.add(own_claim.source_user_message_id)
            action_ids.add(own_claim.action_id)
            log_ids.add(own_claim.log_id)
        # Include referenced rows even when they are orphaned or inconsistent;
        # the terminal validator will reject those facts, never hide them.
        context_rows = list(
            (
                await db.execute(
                    select(
                        AiConversation.conversation_id, AiConversation.user_id
                    ).where(
                        AiConversation.tenant_id == tenant.tenant_id,
                        AiConversation.conversation_id.in_(conversation_ids),
                    )
                )
            ).all()
        )
        source_rows = list(
            (
                await db.execute(
                    select(
                        AiMessage.message_id,
                        AiMessage.conversation_id,
                        AiMessage.role,
                        AiMessage.trace_id,
                    ).where(
                        AiMessage.tenant_id == tenant.tenant_id,
                        AiMessage.message_id.in_(source_ids),
                    )
                )
            ).all()
        )
        bindings = tuple(
            sorted(
                [("conversation", *row) for row in context_rows]
                + [("source", *row) for row in source_rows]
                + [("action", *row) for row in actions]
                + [("log", *row) for row in logs]
            )
        )
        return UserDeleteDependencyPlan(
            tenant.tenant_id,
            targets,
            tuple(sorted(conversation_ids)),
            tuple(sorted(source_ids)),
            tuple(sorted(action_ids)),
            tuple(sorted(log_ids)),
            tuple(sorted(target_action_ids)),
            tuple(sorted(target_log_ids)),
            bindings,
            own_claim,
        )

    async def lock_plan(
        self,
        db: AsyncSession,
        plan: UserDeleteDependencyPlan,
        *,
        tenant: TenantContext,
    ) -> None:
        if plan.tenant_id != tenant.tenant_id:
            raise UserDeleteDependencyChanged()
        await db.execute(select(func.set_config("lock_timeout", "2s", True)))
        for model, column, identifiers in (
            (AiConversation, AiConversation.conversation_id, plan.conversation_ids),
            (AiMessage, AiMessage.message_id, plan.source_ids),
            (AiPreparedAction, AiPreparedAction.action_id, plan.action_ids),
            (AiOperationLog, AiOperationLog.log_id, plan.log_ids),
        ):
            await db.execute(
                select(model)
                .where(model.tenant_id == tenant.tenant_id, column.in_(identifiers))
                .order_by(column)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        await self._recheck(db, plan, tenant=tenant)
        await self._assert_resolved(db, plan, tenant=tenant)
        db.info[_PLAN_KEY] = (db.sync_session.get_transaction(), plan)

    def locked_plan(
        self,
        db: AsyncSession,
        *,
        target_user_ids: Sequence[int],
        tenant: TenantContext,
    ) -> UserDeleteDependencyPlan | None:
        cached = db.info.get(_PLAN_KEY)
        if cached is None or cached[0] is not db.sync_session.get_transaction():
            return None
        plan = cached[1]
        if plan.tenant_id != tenant.tenant_id or plan.target_user_ids != tuple(
            sorted(set(target_user_ids))
        ):
            raise UserDeleteDependencyChanged()
        return plan

    async def recheck_after_user_locks(
        self,
        db: AsyncSession,
        plan: UserDeleteDependencyPlan,
        *,
        tenant: TenantContext,
    ) -> None:
        """Run after User FOR UPDATE; never append a preceding context lock."""
        if (
            self.locked_plan(db, target_user_ids=plan.target_user_ids, tenant=tenant)
            is not plan
        ):
            raise UserDeleteDependencyChanged()
        await self._recheck(db, plan, tenant=tenant)
        await self._assert_resolved(db, plan, tenant=tenant)

    async def _recheck(
        self, db: AsyncSession, plan: UserDeleteDependencyPlan, *, tenant: TenantContext
    ) -> None:
        current = await self.plan(
            db,
            target_user_ids=plan.target_user_ids,
            tenant=tenant,
            own_claim=plan.own_claim,
        )
        if current != plan:
            raise UserDeleteDependencyChanged()

    async def _assert_resolved(
        self, db: AsyncSession, plan: UserDeleteDependencyPlan, *, tenant: TenantContext
    ) -> None:
        actions = list(
            (
                await db.scalars(
                    select(AiPreparedAction)
                    .where(
                        AiPreparedAction.tenant_id == tenant.tenant_id,
                        AiPreparedAction.action_id.in_(plan.target_action_ids),
                    )
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        logs = list(
            (
                await db.scalars(
                    select(AiOperationLog)
                    .where(
                        AiOperationLog.tenant_id == tenant.tenant_id,
                        AiOperationLog.log_id.in_(plan.target_log_ids),
                    )
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        logs_by_call = {log.tool_call_id: log for log in logs}
        proven_logs: set[int] = set()
        for action in actions:
            log = logs_by_call.get(action.execute_tool_call_id)
            if log is None or action.status not in {
                "succeeded",
                "failed",
                "rejected",
                "expired",
            }:
                raise _unresolved()
            readonly = is_registered_readonly_action(action)
            if (
                action.status == "failed"
                and not readonly
                and not (action.execution_owner or "").startswith("v0162-db:")
            ):
                raise _unresolved()
            conversation = await db.scalar(
                select(AiConversation).where(
                    AiConversation.tenant_id == tenant.tenant_id,
                    AiConversation.conversation_id == action.conversation_id,
                )
            )
            source = await db.scalar(
                select(AiMessage).where(
                    AiMessage.tenant_id == tenant.tenant_id,
                    AiMessage.message_id == action.source_user_message_id,
                )
            )
            try:
                validator = (
                    atomic_completion_service.validate_locked_unexecuted_terminal
                    if action.status in {"rejected", "expired"}
                    else self._validate_readonly_terminal
                    if readonly
                    else atomic_completion_service.validate_locked_terminal
                )
                validator(
                    action,
                    log,
                    tenant=replace(tenant, actor_user_id=action.user_id),
                    conversation=conversation,
                    source=source,
                )
            except BusinessRuleException as exc:
                raise _unresolved() from exc
            proven_logs.add(log.log_id)
        for log in logs:
            # A reconciliation annotation can only close by a separately
            # specified protocol. Raw status or parser failure cannot unlock it.
            if "__v0162_reconciliation_v1" in (log.result_summary or ""):
                raise _unresolved()
            from app.modules.ai.service.preflight_completion_service import (  # noqa: PLC0415
                has_preflight_marker,
            )

            if (
                not log.readonly_snapshot or has_preflight_marker(log.result_summary)
            ) and log.log_id not in proven_logs:
                from app.modules.ai.service.preflight_completion_service import (  # noqa: PLC0415
                    is_verified_preflight,
                )

                if await is_verified_preflight(
                    db,
                    log,
                    await db.get(AiConversation, log.conversation_id),
                    await db.get(AiMessage, log.source_user_message_id),
                    tenant_id=tenant.tenant_id,
                    user_id=log.user_id,
                ):
                    continue
                from app.modules.ai.service.preview_completion_service import (  # noqa: PLC0415
                    verify_historical_preview,
                )

                try:
                    await verify_historical_preview(
                        db,
                        log,
                        await db.get(AiConversation, log.conversation_id),
                        await db.get(AiMessage, log.source_user_message_id),
                        tenant_id=tenant.tenant_id,
                        user_id=log.user_id,
                    )
                except (BusinessRuleException, TypeError, ValueError) as exc:
                    raise _unresolved() from exc

    @staticmethod
    def _validate_readonly_terminal(
        action: AiPreparedAction,
        log: AiOperationLog,
        *,
        tenant: TenantContext,
        conversation: AiConversation | None,
        source: AiMessage | None,
    ) -> None:
        """Accept completed registered reads, never a raw readonly log assertion."""
        if (
            not is_registered_readonly_action(action)
            or conversation is None
            or source is None
            or conversation.tenant_id != tenant.tenant_id
            or source.tenant_id != tenant.tenant_id
            or conversation.conversation_id != action.conversation_id
            or conversation.user_id != tenant.actor_user_id
            or source.message_id != action.source_user_message_id
            or source.conversation_id != action.conversation_id
            or source.role != "user"
            or source.trace_id != action.trace_id
            or action.tenant_id != tenant.tenant_id
            or action.user_id != tenant.actor_user_id
            or action.status not in {"succeeded", "failed"}
            or action.row_version < 4
            or action.approval_mode != "hitl"
            or action.dispatch_mode != "inline"
            or action.approved_by != action.user_id
            or action.approved_at is None
            or action.finished_at is None
            or action.execution_owner is not None
            or action.execution_lease_expires_at is not None
            or not action.confirmation_id
            or not action.execute_tool_call_id
            or not action.trace_id
            or not action.agent_code
            or not isinstance(action.frozen_args, dict)
            or not isinstance(action.snapshot, dict)
            or compute_args_hash(action.frozen_args) != action.args_hash
            or compute_args_hash(action.snapshot) != action.snapshot_hash
            or log.started_at is None
            or log.finished_at is None
            or (action.status == "succeeded" and action.error_code is not None)
            or (
                action.status == "failed"
                and (not isinstance(action.error_code, str) or not action.error_code)
            )
        ):
            raise _unresolved()
        expected = {
            "tenant_id": action.tenant_id,
            "user_id": action.user_id,
            "conversation_id": action.conversation_id,
            "source_user_message_id": action.source_user_message_id,
            "trace_id": action.trace_id,
            "agent_code": action.agent_code,
            "tool_name": action.execute_tool_name,
            "tool_call_id": action.execute_tool_call_id,
            "confirmation_id": action.confirmation_id,
            "args_hash": action.args_hash,
            "execution_mode": "hitl",
            "readonly_snapshot": True,
            "status": "success" if action.status == "succeeded" else "failed",
            "approved_by": action.approved_by,
            "error_code": action.error_code,
        }
        if any(getattr(log, field) != value for field, value in expected.items()):
            raise _unresolved()


user_delete_dependency_service = UserDeleteDependencyService()
