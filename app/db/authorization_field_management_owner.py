"""HTTP and controlled-preparation management share the same current-authority owner."""

from app.core.authorization_plan import LockMode, TenantLock, TransactionBudget
from app.core.exceptions import BusinessException
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
from app.modules.system.service.field_management import (
    forbidden,
    prepare_management,
    wire,
)
from app.modules.system.service.generic_field_policy_service import (
    generic_field_policy_service as policies,
)
from app.modules.system.service.user_field_context import load_current_user

PERMISSIONS = {
    **dict.fromkeys(
        (
            "catalog",
            "roles",
            "object",
            "manifest",
            "receipt",
            "preview",
            "manifest-preview",
        ),
        "view",
    ),
    **dict.fromkeys(("publish", "build", "ready"), "edit"),
    **dict.fromkeys(("activate", "freeze"), "activate"),
}


class FieldManagementOwner:
    async def execute(
        self,
        db,
        *,
        tenant,
        actor_user_version,
        catalog,
        operation,
        payload,
        role_id=None,
        object_id=None,
    ):
        require_candidate_session(db)
        writing = operation in {"publish", "build", "freeze", "ready", "activate"}
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
                    required = PERMISSIONS.get(operation)
                    if (
                        not required
                        or not is_super_admin(actor)
                        or f"system:role:field-policy-{required}"
                        not in collect_user_permission_codes(actor)
                    ):
                        raise forbidden()
                    if operation in {"ready", "activate"}:
                        raise BusinessException(
                            409,
                            "全系统入口验收尚未完成",
                            error_code="FIELD_POLICY_ACTIVATION_BLOCKED",
                        )
                    result, command = await prepare_management(
                        db,
                        tx,
                        tenant=tenant,
                        actor=actor,
                        catalog=catalog,
                        operation=operation,
                        payload=payload,
                        role_id=role_id,
                        object_id=object_id,
                    )
                    if command is not None:
                        plan = await policies.plan(
                            db, tx, tenant=tenant, catalog=catalog, command=command
                        )
                        await tx.lock(plan.lock_plan)
                        await load_current_user(
                            db, tenant=tenant, actor_user_version=actor_user_version
                        )
                        result = wire(
                            await policies.apply(
                                db, tx, plan, tenant=tenant, catalog=catalog
                            )
                        )
        except BaseException:
            await rollback_authorization_session(db)
            raise
        if writing:
            await commit_authorization_session(db)
        else:
            await rollback_authorization_session(db)
        return result
