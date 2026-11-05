"""Single-plan tenant bootstrap coordinator with lock-free password preparation."""

from copy import deepcopy

from app.core.authorization_io import hash_password
from app.core.authorization_plan import LockMode, LockPlan, TransactionBudget
from app.core.exceptions import BusinessRuleException
from app.db.authorization_plan_session import _order_error
from app.db.authorization_system_owner import require_candidate_session
from app.modules.ai.service.authorization_bootstrap_candidate import (
    ai_bootstrap_candidate,
)
from app.modules.platform.tenant_bootstrap_service import (
    TenantBootstrapResult,
    _bootstrap_fingerprint,
)
from app.modules.system.service.authorization_bootstrap_candidate import (
    system_bootstrap_candidate,
)
from app.modules.system.service.authorization_control_identity import (
    ControlAudit,
    assert_identity,
    read_identity,
)
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_tenant_candidate import _barriers
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.tenant_lifecycle_service import (
    hash_tenant_idempotency_key,
)
from app.utils.validators import PWD_ERROR_CODE, PWD_ERROR_MSG, validate_password


class TenantBootstrapCandidate:
    async def execute(
        self, db, *, tenant, actor_user_version, target_tenant_id, payload, audit
    ):
        require_candidate_session(db)
        if (
            type(target_tenant_id) is not int
            or not 0 < target_tenant_id < 2**63
            or type(audit) is not ControlAudit
            or type(payload) is not dict
            or set(payload) != {"default_model_id", "admin_password", "idempotency_key"}
        ):
            raise _order_error()
        audit.assert_request(
            tenant=tenant,
            actor_user_version=actor_user_version,
            operation="tenant.bootstrap",
            permission="platform:tenant:bootstrap",
            target_tenant_id=target_tenant_id,
        )
        payload = deepcopy(payload)
        model_id = payload["default_model_id"]
        if type(model_id) is not int or not 0 < model_id < 2**63:
            raise BusinessRuleException(
                "defaultModelId 无效",
                error_code="PLATFORM_TENANT_BOOTSTRAP_MODEL_ID_INVALID",
            )
        try:
            validate_password(payload["admin_password"])
        except (TypeError, ValueError) as exc:
            raise BusinessRuleException(
                PWD_ERROR_MSG, error_code=PWD_ERROR_CODE
            ) from exc
        key_hash = hash_tenant_idempotency_key(payload["idempotency_key"])
        fingerprint = _bootstrap_fingerprint(
            tenant_id=target_tenant_id,
            default_model_id=model_id,
            admin_password=payload["admin_password"],
        )
        password_hash = await hash_password(payload["admin_password"])
        seeds = system_bootstrap_candidate.seeds(target_tenant_id, password_hash)
        with bounded_authorization_reads(db):
            async with authorization_transactions.scope(
                db,
                tenants=_barriers(tenant.tenant_id, target_tenant_id),
                global_mode=LockMode.EXCLUSIVE,
                budget=TransactionBudget(timeout_ms=60000),
            ) as tx:
                identity = await read_identity(
                    db,
                    tenant=tenant,
                    actor_user_version=actor_user_version,
                    permissions=("platform:tenant:bootstrap",),
                    system=True,
                )
                system = await system_bootstrap_candidate.prepare(
                    db,
                    tenant_id=target_tenant_id,
                    key_hash=key_hash,
                    fingerprint=fingerprint,
                    seeds=seeds,
                )
                ai = await ai_bootstrap_candidate.prepare(
                    db,
                    tenant_id=target_tenant_id,
                    model_id=model_id,
                    super_role_id=system.super_role_id,
                    replayed=system.replayed,
                )
                await tx.lock(
                    LockPlan(
                        existing=tuple(
                            set(
                                identity.keys
                                + system.existing
                                + ai.existing
                                + audit.existing
                            )
                        ),
                        new=system.new + ai.new + audit.new,
                    )
                )
                await assert_identity(db, identity)
                current_system = await system_bootstrap_candidate.prepare(
                    db,
                    tenant_id=target_tenant_id,
                    key_hash=key_hash,
                    fingerprint=fingerprint,
                    seeds=seeds,
                )
                current_ai = await ai_bootstrap_candidate.prepare(
                    db,
                    tenant_id=target_tenant_id,
                    model_id=model_id,
                    super_role_id=system.super_role_id,
                    replayed=system.replayed,
                )
                if current_system != system or current_ai != ai:
                    raise BusinessRuleException(
                        "引导资源已变化", error_code="AUTHORIZATION_SNAPSHOT_STALE"
                    )
                async with tx.aggregate(target_tenant_id):
                    await system_bootstrap_candidate.apply(tx, system)
                    await ai_bootstrap_candidate.apply(tx, ai)
                await audit.append(
                    tx, {"tenantId": str(target_tenant_id), "replayed": system.replayed}
                )
        return TenantBootstrapResult(
            system.tenant_code,
            system.lifecycle_state,
            "admin",
            ai.model_label,
            system.menu_count,
            2,
            1,
            ai.agent_binding_count,
            system.replayed,
        )


tenant_bootstrap_candidate = TenantBootstrapCandidate()
