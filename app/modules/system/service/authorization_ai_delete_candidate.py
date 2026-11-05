"""Frozen user deletion with explicit historical dependency proofs."""

from dataclasses import dataclass
from types import SimpleNamespace

from sqlalchemy import or_, select

from app.core.authorization_plan import LockMode, LockPlan, ResourceKey, TenantLock
from app.core.authorization_plan import ResourceKind as K
from app.db.authorization_plan_session import _order_error
from app.modules.ai.agents.gateway.result import ResultProjection, ToolResult, UIResult
from app.modules.ai.service.atomic_completion_service import FILE_TOOLS
from app.modules.ai.service.authorization_context_candidate import ai_context_candidate
from app.modules.ai.service.user_delete_dependency_service import (
    _unresolved,
    user_delete_dependency_service,
)
from app.modules.system.models.user import User
from app.modules.system.service.authorization_ai_user_input import (
    context,
    ensure_targets_in_scope,
)
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_resource_catalog import resource_spec
from app.modules.system.service.authorization_system_candidate import (
    _ids,
    _limit,
)
from app.modules.system.service.authorization_system_candidate import (
    system_authorization_candidate as system,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.user_service import user_service


@dataclass(slots=True, repr=False)
class _DeleteWork:
    transaction: object
    command: object
    domain: object
    contexts: object
    facts: dict
    locks: LockPlan
    http: bool = False
    used: bool = False


class AIDeleteCandidate:
    @staticmethod
    def _ids(command):
        _, _, operation, payload, target, *_ = command.values
        if operation not in {"user.delete", "user.batch_delete"}:
            raise _order_error()
        return _ids(payload if operation == "user.batch_delete" else [target])

    async def _facts(self, db, tenant, ids):
        tid = tenant.tenant_id
        table = resource_spec(K.CONVERSATION).table
        conversations = [
            dict(r)
            for r in (
                await db.execute(
                    select(table)
                    .where(table.c.tenant_id == tid, table.c.user_id.in_(ids))
                    .limit(10001)
                )
            ).mappings()
        ]
        cids = tuple(r["conversation_id"] for r in conversations)
        if len(conversations) > 10000:
            raise _limit()
        facts = {K.CONVERSATION: conversations}
        for kind in (
            K.SOURCE_MESSAGE,
            K.PREPARED_ACTION,
            K.OPERATION_LOG,
            K.ROUTING_LOG,
            K.ROUTING_FEEDBACK,
        ):
            table = resource_spec(kind).table
            selectors = []
            if "conversation_id" in table.c:
                selectors.append(table.c.conversation_id.in_(cids))
            if "user_id" in table.c:
                selectors.append(table.c.user_id.in_(ids))
            if kind is K.ROUTING_FEEDBACK:
                selectors.append(
                    table.c.message_id.in_(
                        [r["message_id"] for r in facts[K.SOURCE_MESSAGE]]
                    )
                )
            facts[kind] = [
                dict(r)
                for r in (
                    await db.execute(
                        select(table)
                        .where(table.c.tenant_id == tid, or_(*selectors))
                        .order_by(
                            *[table.c[n] for n in resource_spec(kind).key_columns]
                        )
                        .limit(10001)
                    )
                ).mappings()
            ]
            if len(facts[kind]) > 10000:
                raise _limit()
            if any(
                "conversation_id" in r
                and r["conversation_id"] is not None
                and r["conversation_id"] not in cids
                for r in facts[kind]
            ):
                raise _unresolved()
        # RESTRICT-owned transfers are retained until their separate FILE owner
        # has completed cleanup. A user deletion never guesses file disposition.
        for kind in (K.IMPORT_BATCH, K.EXPORT_TASK):
            table = resource_spec(kind).table
            if (
                await db.execute(
                    select(table.c.operator_id)
                    .where(table.c.tenant_id == tid, table.c.operator_id.in_(ids))
                    .limit(1)
                )
            ).first():
                raise _unresolved()
        if any(r["execute_tool_name"] in FILE_TOOLS for r in facts[K.PREPARED_ACTION]):
            raise _unresolved()
        bound = {r["execute_tool_call_id"] for r in facts[K.PREPARED_ACTION]}
        if any(row["tool_call_id"] not in bound for row in facts[K.OPERATION_LOG]):
            # A raw readonly flag is not a historical completion proof.
            raise _unresolved()
        return facts

    async def _check_targets(self, db, command):
        tenant, _, _, ids, _, snapshot, *_ = command.values
        ctx = await context(db, tenant, "user.batch_delete")
        await ensure_targets_in_scope(ctx, user_ids=ids)
        users = list(
            await db.scalars(
                select(User)
                .where(User.tenant_id == tenant.tenant_id, User.user_id.in_(ids))
                .execution_options(populate_existing=True)
            )
        )
        if {u.user_id for u in users} != set(
            ids
        ) or user_service.build_batch_delete_identity_snapshot(users) != snapshot:
            raise _order_error()

    async def plan(self, db, tx, command, *, http=False):
        tenant = command.values[0]
        ids = self._ids(command)
        tx.assert_planning(tenant.tenant_id, exclusive=True)
        if http:
            if command.values[5] is not None:
                raise _order_error()
        else:
            await self._check_targets(db, command)
        domain = await system.plan(db, tx, command)
        facts = await self._facts(db, tenant, ids)
        contexts = await ai_context_candidate.discover(
            db,
            tx,
            tenant_id=tenant.tenant_id,
            conversation_ids=tuple(r["conversation_id"] for r in facts[K.CONVERSATION]),
        )
        keys = set(domain.locks.existing) | set(contexts.keys)
        for kind, rows in facts.items():
            for row in rows:
                actual = (
                    K.ASSISTANT_MESSAGE
                    if kind is K.SOURCE_MESSAGE and row["role"] != "user"
                    else kind
                )
                keys.add(
                    ResourceKey(
                        actual,
                        tenant.tenant_id,
                        tuple(row[n] for n in resource_spec(kind).key_columns),
                    )
                )
        return _DeleteWork(
            tx,
            command,
            domain,
            contexts,
            facts,
            LockPlan(existing=tuple(keys), new=domain.locks.new),
            http=http,
        )

    async def apply(self, db, tx, command, work):
        tx.assert_session(db)
        if (
            type(work) is not _DeleteWork
            or work.transaction is not tx
            or work.command != command
            or work.used
        ):
            raise _order_error()
        work.used = True
        tx.assert_planned(work.locks)
        tenant = command.values[0]
        ids = self._ids(command)
        if not work.http:
            await self._check_targets(db, command)
        await ai_context_candidate.recheck(db, tx, work.contexts)
        if await self._facts(db, tenant, ids) != work.facts:
            raise _unresolved()
        await user_delete_dependency_service._assert_resolved(
            db,
            SimpleNamespace(
                target_action_ids=tuple(
                    r["action_id"] for r in work.facts[K.PREPARED_ACTION]
                ),
                target_log_ids=tuple(r["log_id"] for r in work.facts[K.OPERATION_LOG]),
            ),
            tenant=tenant,
        )
        for kind in (
            K.ROUTING_FEEDBACK,
            K.ROUTING_LOG,
            K.OPERATION_LOG,
            K.PREPARED_ACTION,
            K.SOURCE_MESSAGE,
            K.CONVERSATION,
        ):
            for row in work.facts[kind]:
                actual = (
                    K.ASSISTANT_MESSAGE
                    if kind is K.SOURCE_MESSAGE and row["role"] != "user"
                    else kind
                )
                await tx.delete_context_fact(
                    ResourceKey(
                        actual,
                        tenant.tenant_id,
                        tuple(row[n] for n in resource_spec(kind).key_columns),
                    ),
                    expected=row,
                )
        changed = await system.apply(db, tx, work.domain)
        if work.http:
            return changed
        count = changed["result"]
        str_ids = [str(n) for n in sorted(ids)]
        return ToolResult.success(
            data={"deleted": count},
            projection=ResultProjection(
                subject_refs=tuple({"type": "user", "id": n} for n in str_ids)
            ),
            ui=UIResult(
                view_type="rows_affected",
                view_data={"count": count, "ids": str_ids},
                audit={"affected_user_ids": str_ids},
                label_key="ai.tool.user.batch_delete.result",
                label_params={"count": count},
            ),
        )

    async def execute_http(self, db, **request):
        command = await system.prepare_input(db, **request)
        tenant = command.values[0]
        with bounded_authorization_reads(db):
            async with authorization_transactions.scope(
                db, tenants=(TenantLock(tenant.tenant_id, LockMode.EXCLUSIVE),)
            ) as tx:
                work = await self.plan(db, tx, command, http=True)
                await tx.lock(work.locks)
                return await self.apply(db, tx, command, work)


ai_delete_candidate = AIDeleteCandidate()
