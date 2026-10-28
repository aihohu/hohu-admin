"""Pending decisions commit before a separate, original-claim execution owner."""

import asyncio
from copy import deepcopy

from app.core.authorization_plan import LockMode, TenantLock, TransactionBudget
from app.core.exceptions import BusinessException
from app.core.file_storage import ArtifactExecution
from app.core.id_generator import next_id
from app.db.authorization_ai_owner import AIAuthorizationOwner
from app.db.authorization_owner import (
    authorization_session,
    commit_authorization_session,
    rollback_authorization_session,
)
from app.db.authorization_plan_session import _order_error
from app.modules.ai.agents.gateway.authorization_execution import PlannedGatewayOwner
from app.modules.ai.service.atomic_completion_service import new_execution_owner
from app.modules.ai.service.authorization_execution_candidate import (
    ExecutionDisposition,
)
from app.modules.ai.service.authorization_lifecycle_candidate import lifecycle_candidate
from app.modules.system.service.authorization_ai_tool_adapter import (
    system_ai_tool_adapter,
)
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)


class PendingConfirmationRevalidationFailed(BusinessException):
    """A pre-claim rejection whose owning transaction has rolled back."""

    def __init__(self, denial):
        super().__init__(denial.code, denial.message, error_code=denial.error_code)
        self.denial = denial


class LifecycleOwner:
    def __init__(self, factory):
        self.factory = factory

    async def prepare(
        self,
        *,
        tool,
        args,
        snapshot,
        tenant,
        actor_user_version,
        conversation_id,
        source_id,
        trace_id,
        dry_run=False,
    ):
        args, snapshot = deepcopy(args), deepcopy(snapshot)
        execution = ArtifactExecution(
            tenant.tenant_id, str(next_id()), new_execution_owner(tool)
        )
        async with self.factory() as db:
            try:
                command = await system_ai_tool_adapter.prepare_input(
                    db,
                    tool=tool,
                    args=args,
                    snapshot=snapshot,
                    tenant=tenant,
                    actor_user_version=actor_user_version,
                    execution=execution,
                )
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(tenant.tenant_id, LockMode.EXCLUSIVE),),
                        budget=TransactionBudget(timeout_ms=15000),
                    ) as tx:
                        result = await lifecycle_candidate.prepare(
                            db,
                            tx,
                            command,
                            tenant=tenant,
                            actor_user_version=actor_user_version,
                            tool=tool,
                            args=args,
                            snapshot=snapshot,
                            conversation_id=conversation_id,
                            source_id=source_id,
                            trace_id=trace_id,
                            dry_run=dry_run,
                        )
            except BaseException:
                await rollback_authorization_session(db)
                raise
            if dry_run:
                await rollback_authorization_session(db)
            else:
                await commit_authorization_session(db)
            return result

    async def admit(
        self,
        confirmation_id,
        *,
        tenant,
        actor_user_version,
        decision,
        disabled=False,
        denial=None,
        resume_binding=None,
    ):
        async with self.factory() as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(tenant.tenant_id),),
                        budget=TransactionBudget(timeout_ms=8000),
                    ) as tx:
                        result = await lifecycle_candidate.admit(
                            db,
                            tx,
                            confirmation_id,
                            tenant=tenant,
                            actor_user_version=actor_user_version,
                            decision=decision,
                            disabled=disabled,
                            denial=denial,
                            resume_binding=resume_binding,
                        )
                db.expunge(result.action)
            except BaseException:
                await rollback_authorization_session(db)
                raise
            if result.changed:
                await commit_authorization_session(db)
            else:
                await rollback_authorization_session(db)
            return result

    async def unexecuted(self, confirmation_id, *, tenant):
        async with self.factory() as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(tenant.tenant_id),),
                        budget=TransactionBudget(timeout_ms=8000),
                    ) as tx:
                        action = await lifecycle_candidate.unexecuted(
                            db, tx, confirmation_id, tenant=tenant
                        )
                        db.expunge(action)
                        return action
            finally:
                await rollback_authorization_session(db)

    async def resume_read(self, confirmation_id, **request):
        async with authorization_session(self.factory) as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(request["tenant"].tenant_id),),
                        budget=TransactionBudget(timeout_ms=8000),
                    ) as tx:
                        return await lifecycle_candidate.resume_read(
                            db, tx, confirmation_id, **request
                        )
            finally:
                await rollback_authorization_session(db)

    async def confirm(self, confirmation_id, *, tenant, actor_user_version):
        async with self.factory() as db:
            try:
                with bounded_authorization_reads(db):
                    async with asyncio.timeout(5):
                        action = await lifecycle_candidate.reference(
                            db, confirmation_id, tenant=tenant
                        )
                        if action is None:
                            raise _order_error()
                        status, identifier, tool, args, snapshot = (
                            action.status,
                            action.action_id,
                            action.execute_tool_name,
                            deepcopy(action.frozen_args),
                            deepcopy(action.snapshot),
                        )
            finally:
                await rollback_authorization_session(db)
            if status != "pending_confirmation":
                fact = await AIAuthorizationOwner(self.factory).reconcile(
                    confirmation_id, tenant=tenant
                )
                return ExecutionDisposition(fact.disposition)
            owner = new_execution_owner(tool)
            try:
                async with asyncio.timeout(5):
                    command = await system_ai_tool_adapter.prepare_input(
                        db,
                        tool=tool,
                        args=args,
                        snapshot=(
                            {k: v for k, v in snapshot.items() if k != "_authorization"}
                            if tool == "user.import_execute"
                            else snapshot.get("business")
                        ),
                        tenant=tenant,
                        actor_user_version=actor_user_version,
                        execution=ArtifactExecution(
                            tenant.tenant_id, str(identifier), owner
                        ),
                    )
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(tenant.tenant_id, LockMode.EXCLUSIVE),),
                        budget=TransactionBudget(timeout_ms=15000),
                    ) as tx:
                        claim = await lifecycle_candidate.confirm(
                            db,
                            tx,
                            tenant=tenant,
                            actor_user_version=actor_user_version,
                            confirmation_id=confirmation_id,
                            command=command,
                            owner=owner,
                        )
            except BaseException as exc:
                await rollback_authorization_session(db)
                # No claim COMMIT was attempted. Only a domain rejection with
                # known rollback may ask a new owner to prove never-started.
                if (
                    isinstance(exc, BusinessException)
                    and 400 <= exc.code < 500
                    and not exc.error_code.startswith("AUTHORIZATION_")
                ):
                    raise PendingConfirmationRevalidationFailed(exc) from exc
                raise
            await commit_authorization_session(db)
        outcome = await PlannedGatewayOwner(self.factory).execute_claim(
            claim, tenant=tenant, actor_user_version=actor_user_version
        )
        return ExecutionDisposition(outcome.disposition, claim)

    async def decide(self, confirmation_id, *, tenant, actor_user_version, decision):
        async with self.factory() as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(tenant.tenant_id),),
                        budget=TransactionBudget(timeout_ms=8000),
                    ) as tx:
                        result = await lifecycle_candidate.decide(
                            db,
                            tx,
                            tenant=tenant,
                            actor_user_version=actor_user_version,
                            confirmation_id=confirmation_id,
                            decision=decision,
                        )
            except BaseException:
                await rollback_authorization_session(db)
                raise
            await commit_authorization_session(db)
        return result
