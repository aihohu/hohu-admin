"""Pending decisions commit before a separate, original-claim execution owner."""

import asyncio
from copy import deepcopy

from app.core.authorization_plan import LockMode, TenantLock, TransactionBudget
from app.core.file_storage import ArtifactExecution
from app.core.id_generator import next_id
from app.db.authorization_ai_owner import AIAuthorizationOwner
from app.db.authorization_owner import (
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
            except BaseException:
                await rollback_authorization_session(db)
                raise
            await commit_authorization_session(db)
        return await PlannedGatewayOwner(self.factory).execute_claim(
            claim, tenant=tenant, actor_user_version=actor_user_version
        )

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
