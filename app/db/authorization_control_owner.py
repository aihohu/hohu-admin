"""Explicit control-plane owners; ordinary production entry points stay unchanged."""

import asyncio
from copy import deepcopy
from functools import partial

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.authorization_plan import (
    LockPlan,
    NewResource,
    ResourceKey,
    ResourceKind,
    TenantLock,
    TransactionBudget,
)
from app.core.exceptions import BusinessException
from app.core.redis import redis_client
from app.db.authorization_owner import (
    PostCommitEffects,
    commit_authorization_session,
    rollback_authorization_session,
)
from app.db.authorization_plan_session import AuthorizationPlanSession, _order_error
from app.db.authorization_system_owner import require_candidate_session
from app.modules.ai.service.authorization_catalog_candidate import (
    catalog_authorization_candidate,
)
from app.modules.ai.service.authorization_catalog_inputs import (
    CATALOG_CANDIDATE_ENTRIES,
    DNS_OPERATIONS,
    PROBE_OPERATIONS,
)
from app.modules.platform.authorization_bootstrap_candidate import (
    tenant_bootstrap_candidate,
)
from app.modules.system.service.authorization_configuration_candidate import (
    CONFIGURATION_PERMISSIONS,
    ConfigurationExecution,
    configuration_authorization_candidate,
)
from app.modules.system.service.authorization_control_audit import issue_control_audit
from app.modules.system.service.authorization_control_identity import (
    assert_identity,
    read_identity,
)
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_tenant_candidate import (
    TENANT_OPERATIONS,
    tenant_control_candidate,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)


async def _rollback_preserving(db, error):
    try:
        await rollback_authorization_session(db)
    except Exception:
        if isinstance(error, asyncio.CancelledError):
            raise error
        raise


class ControlCandidateOwner:
    async def _failure(self, factory, audit, error):
        async with asyncio.timeout(3):
            async with factory() as db:
                require_candidate_session(db)
                try:
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(audit.tenant.tenant_id),),
                        budget=TransactionBudget(timeout_ms=3000),
                    ) as tx:
                        await tx.lock(LockPlan(existing=audit.existing, new=audit.new))
                        status = getattr(error, "code", 500)
                        if type(status) is not int or not 400 <= status <= 599:
                            status = 500
                        await tx.append_audit(
                            audit.new[0].key,
                            audit.values(
                                event_type="completed",
                                status_code=status,
                                result_summary={"statusCode": status},
                            ),
                        )
                except BaseException as failure:
                    await _rollback_preserving(db, failure)
                    raise
                await commit_authorization_session(db)

    async def execute(
        self,
        db,
        *,
        tenant,
        actor_user_version,
        operation,
        payload=None,
        target_id=None,
        target_tenant_id=None,
        provider_id=None,
        expected_snapshot=None,
        audit_context=None,
        audit_factory=None,
    ):
        """Own all phases; external DNS/SMTP/probes never borrow business locks."""

        require_candidate_session(db)
        payload, expected_snapshot, audit_context = deepcopy(
            (payload, expected_snapshot, audit_context)
        )
        catalog = (
            operation in CATALOG_CANDIDATE_ENTRIES or operation in PROBE_OPERATIONS
        )
        config = operation in CONFIGURATION_PERMISSIONS or operation == "mail.test"
        bootstrap = operation == "tenant.bootstrap"
        lifecycle = operation in TENANT_OPERATIONS
        if not (catalog or config or bootstrap or lifecycle):
            raise _order_error()
        global_config = operation in {"mail.save", "mail.test"} or (
            operation == "settings.save"
            and type(payload) is dict
            and payload.get("group") == "security"
        )
        audited = (
            (catalog and operation != "role.agents")
            or global_config
            or bootstrap
            or lifecycle
        )
        permission = (
            "platform:ai:write"
            if catalog
            else "platform:tenant:bootstrap"
            if bootstrap
            else TENANT_OPERATIONS[operation]
            if lifecycle
            else "system:setting:edit"
            if operation == "mail.test"
            else CONFIGURATION_PERMISSIONS[operation]
        )
        audit = None
        factory = audit_factory
        if audited:
            if type(audit_context) is not dict or set(audit_context) != {
                "reason",
                "ticket_id",
                "correlation_id",
            }:
                raise _order_error()
            if factory is None:
                if not isinstance(db.bind, AsyncEngine):
                    raise _order_error()
                factory = async_sessionmaker(
                    db.bind,
                    class_=AuthorizationPlanSession,
                    expire_on_commit=False,
                    autoflush=False,
                )
            async with factory() as intent_db:
                if intent_db is db:
                    raise _order_error()
                audit = await self.authorize(
                    intent_db,
                    tenant=tenant,
                    actor_user_version=actor_user_version,
                    operation=operation,
                    permission=permission,
                    target_tenant_id=target_tenant_id,
                    path="/authorization-candidates/" + operation,
                    method="POST",
                    **audit_context,
                )
        effects = PostCommitEffects()
        try:
            request = {
                "tenant": tenant,
                "actor_user_version": actor_user_version,
                "operation": operation,
                "payload": payload,
                "target_id": target_id,
                "target_tenant_id": target_tenant_id,
                "expected_snapshot": expected_snapshot,
            }
            if catalog:
                request["provider_id"] = provider_id
                if operation in PROBE_OPERATIONS:
                    prepared = await catalog_authorization_candidate.prepare_probe(
                        db, **request
                    )
                    await rollback_authorization_session(db)
                    probe = await catalog_authorization_candidate.run_probe(prepared)
                    result = await catalog_authorization_candidate.complete_probe(
                        db, preparation=prepared, result=probe, audit=audit
                    )
                else:
                    prepared = None
                    if operation in DNS_OPERATIONS:
                        prepared = await catalog_authorization_candidate.prepare(
                            db, **request
                        )
                        await rollback_authorization_session(db)
                        await catalog_authorization_candidate.validate_preparation(
                            prepared
                        )
                    result = await catalog_authorization_candidate.execute(
                        db, **request, audit=audit, preparation=prepared
                    )
            elif operation == "mail.test":
                prepared = (
                    await configuration_authorization_candidate.prepare_mail_test(
                        db,
                        tenant=tenant,
                        actor_user_version=actor_user_version,
                        payload=payload,
                        audit=audit,
                    )
                )
                await rollback_authorization_session(db)
                outcome = await configuration_authorization_candidate.run_mail_test(
                    prepared
                )
                result = await configuration_authorization_candidate.complete_mail_test(
                    db,
                    tenant=tenant,
                    actor_user_version=actor_user_version,
                    preparation=prepared,
                    outcome=outcome,
                    audit=audit,
                )
            elif config:
                if operation == "config.import":
                    request[
                        "payload"
                    ] = await configuration_authorization_candidate.parse_import(
                        payload
                    )
                execution = await configuration_authorization_candidate.execute(
                    db, **request, audit=audit
                )
                if type(execution) is not ConfigurationExecution:
                    raise _order_error()
                for key in execution.cache_keys:
                    effects.cache(key, partial(redis_client.delete, key))
                result = (
                    execution.value
                    if execution.cache_complete
                    else {
                        "result": execution.value,
                        "cache_refresh": "deferred",
                        "cache_ttl_seconds": 300,
                    }
                )
            elif bootstrap:
                result = await tenant_bootstrap_candidate.execute(
                    db,
                    tenant=tenant,
                    actor_user_version=actor_user_version,
                    target_tenant_id=target_tenant_id,
                    payload=payload,
                    audit=audit,
                )
            else:
                result = await tenant_control_candidate.execute(
                    db,
                    tenant=tenant,
                    actor_user_version=actor_user_version,
                    operation=operation,
                    target_tenant_id=target_tenant_id,
                    payload=payload,
                    audit=audit,
                )
        except BaseException as error:
            effects.discard()
            await _rollback_preserving(db, error)
            if audit is not None:
                try:
                    await self._failure(factory, audit, error)
                except BaseException:
                    if isinstance(error, asyncio.CancelledError):
                        raise error
                    raise BusinessException(
                        503,
                        "失败完成审计暂不可用",
                        error_code="PLATFORM_AUDIT_UNAVAILABLE",
                    ) from error
            raise
        # ACK failures are deliberately outside the business rollback handler.
        await commit_authorization_session(db, effects)
        return result

    async def authorize(
        self,
        db,
        *,
        tenant,
        actor_user_version,
        operation,
        permission,
        reason,
        ticket_id,
        correlation_id,
        target_tenant_id,
        path,
        method,
    ):
        """Own an independent, required intent transaction before business locks."""
        require_candidate_session(db)
        async with asyncio.timeout(3):
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(tenant.tenant_id),),
                        budget=TransactionBudget(timeout_ms=3000),
                    ) as tx:
                        identity = await read_identity(
                            db,
                            tenant=tenant,
                            actor_user_version=actor_user_version,
                            permissions=(permission,),
                            system=True,
                        )
                        audit = issue_control_audit(
                            tenant=tenant,
                            actor_user_version=actor_user_version,
                            actor_name=identity.user_name,
                            operation=operation,
                            permission=permission,
                            reason=reason,
                            ticket_id=ticket_id,
                            correlation_id=correlation_id,
                            target_tenant_id=target_tenant_id,
                            path=path,
                            method=method,
                        )
                        key = ResourceKey(
                            ResourceKind.SYSTEM_OPERATION_LOG,
                            tenant.tenant_id,
                            (audit.authorization_audit_id,),
                        )
                        await tx.lock(
                            LockPlan(existing=identity.keys, new=(NewResource(key),))
                        )
                        await assert_identity(db, identity)
                        await tx.append_audit(
                            key, audit.values(event_type="authorized")
                        )
            except BaseException as error:
                await _rollback_preserving(db, error)
                raise
            await commit_authorization_session(db)
        return audit


control_candidate_owner = ControlCandidateOwner()
