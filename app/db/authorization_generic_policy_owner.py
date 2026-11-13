"""Authenticated generic management owner; policies never commit their own work."""

from app.core.authorization_plan import LockMode, TenantLock, TransactionBudget
from app.core.exceptions import AuthorizationException
from app.core.field_policy.documents import catalog_document
from app.core.rbac import is_super_admin
from app.db.authorization_owner import (
    commit_authorization_session,
    rollback_authorization_session,
)
from app.db.authorization_system_owner import require_candidate_session
from app.modules.auth.permission_collect import collect_user_permission_codes
from app.modules.system.service.authorization_read_budget import (
    AuthorizationReadBudget,
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.generic_field_policy_service import (
    generic_field_policy_service as policies,
)
from app.modules.system.service.user_field_context import load_current_user

PERMISSIONS = {
    "read": "system:role:field-policy-view",
    "receipt": "system:role:field-policy-view",
    "publish": "system:role:field-policy-edit",
    "archive": "system:role:field-policy-edit",
    "build": "system:role:field-policy-edit",
    "ready": "system:role:field-policy-edit",
    "activate": "system:role:field-policy-activate",
    "freeze": "system:role:field-policy-activate",
}


class GenericFieldPolicyOwner:
    async def execute(
        self, db, *, tenant, actor_user_version, catalog, command, qualification=None
    ):
        require_candidate_session(db)
        action = command.get("action") if type(command) is dict else None
        writing = action not in {"read", "receipt"}
        try:
            with bounded_authorization_reads(
                db, AuthorizationReadBudget(max_bytes=128 * 1024 * 1024)
            ):
                async with authorization_transactions.scope(
                    db,
                    tenants=(
                        TenantLock(
                            tenant.tenant_id,
                            LockMode.EXCLUSIVE if writing else LockMode.SHARED,
                        ),
                    ),
                    budget=TransactionBudget(timeout_ms=15000 if writing else 8000),
                ) as tx:
                    actor = await load_current_user(
                        db, tenant=tenant, actor_user_version=actor_user_version
                    )
                    required = PERMISSIONS.get(action)
                    if (
                        required is None
                        or not is_super_admin(actor)
                        or required not in collect_user_permission_codes(actor)
                    ):
                        raise AuthorizationException(
                            "字段策略管理权限不足",
                            error_code="FIELD_POLICY_MANAGEMENT_FORBIDDEN",
                        )
                    if writing:
                        plan = await policies.plan(
                            db,
                            tx,
                            tenant=tenant,
                            catalog=catalog,
                            command=command,
                            qualification=qualification,
                        )
                        await tx.lock(plan.lock_plan)
                        await load_current_user(
                            db, tenant=tenant, actor_user_version=actor_user_version
                        )
                        result = await policies.apply(
                            db, tx, plan, tenant=tenant, catalog=catalog
                        )
                    elif action == "receipt":
                        if set(command) != {"action", "request_id"}:
                            raise AuthorizationException("无效策略读取")
                        result = await policies.receipt(
                            db, tenant=tenant, request_id=command["request_id"]
                        )
                    else:
                        if set(command) != {"action"}:
                            raise AuthorizationException("无效策略读取")
                        await policies._catalog(db, catalog)
                        result = {
                            "schema_version": 2,
                            "catalog_digest": catalog.digest,
                            "catalog": catalog_document(catalog),
                            "head": dict(await policies._head(db, tenant.tenant_id)),
                            "objects": [
                                dict(row)
                                for row in await policies._objects(
                                    db, tenant.tenant_id, catalog
                                )
                            ],
                            "bindings": [
                                dict(row)
                                for row in await policies._bindings(
                                    db, tenant.tenant_id, catalog
                                )
                            ],
                        }
        except BaseException:
            await rollback_authorization_session(db)
            raise
        if writing:
            await commit_authorization_session(db)
        else:
            await rollback_authorization_session(db)
        return result
