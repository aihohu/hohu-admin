"""Fixed public policy owner: current authority, complete plan, sole ACK boundary."""

import asyncio
from uuid import uuid4

from app.core.authorization_plan import LockMode, TenantLock, TransactionBudget
from app.core.exceptions import BusinessException
from app.db.authorization_owner import (
    commit_authorization_session,
    rollback_authorization_session,
)
from app.db.authorization_system_owner import require_candidate_session
from app.modules.system.field_policy.qualification import qualification_blockers
from app.modules.system.service.authorization_read_budget import (
    AuthorizationReadBudget,
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.generic_field_policy_admission import (
    require_user_policy_compatibility,
)
from app.modules.system.service.planned_user_field_policy_service import (
    planned_user_field_policy_service as policies,
)
from app.modules.system.service.user_field_policy_admin import (
    policy_catalog,
    policy_rule_diff,
    policy_wire,
    state_wire,
)
from app.modules.system.service.user_field_policy_admin import (
    user_field_policy_admin as admin,
)
from app.modules.system.service.user_field_policy_preview import effective_preview


class UserFieldPolicyOwner:
    async def execute(
        self, db, *, tenant, actor_user_version, operation, role_id, payload=None
    ):
        require_candidate_session(db)
        writing = operation in {"publish", "ready", "activate", "freeze"}
        blockers = await asyncio.to_thread(qualification_blockers)
        try:
            with bounded_authorization_reads(
                db,
                AuthorizationReadBudget(
                    max_bytes=(128 if writing else 64) * 1024 * 1024
                ),
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
                    await admin.authorize(
                        db,
                        tx,
                        tenant=tenant,
                        actor_user_version=actor_user_version,
                        operation=operation,
                    )
                    await require_user_policy_compatibility(
                        db, tenant_id=tenant.tenant_id
                    )
                    state, snapshot = await admin.read(
                        db, tx, tenant=tenant, role_id=role_id
                    )
                    if operation == "activate" and blockers:
                        raise BusinessException(
                            409,
                            "完整入口及激活验收尚未通过",
                            error_code="FIELD_POLICY_ACTIVATION_BLOCKED",
                        )
                    if operation in {"preview", "publish"}:
                        grants = admin.validate_draft(payload, snapshot, role_id)
                    if operation == "preview":
                        result = {
                            **await effective_preview(
                                db,
                                tx,
                                tenant=tenant,
                                role_id=role_id,
                                grants=grants,
                                state=state,
                            ),
                            "expectedVersion": payload.expected_version,
                            "changes": policy_rule_diff(
                                snapshot.rules if snapshot else (), grants
                            ),
                            "previewKind": "rule-diff",
                            "state": state_wire(state, blockers),
                        }
                    elif writing:
                        plan = await policies.plan(
                            db,
                            tx,
                            tenant=tenant,
                            action=operation,
                            role_id=role_id if operation == "publish" else None,
                        )
                        await tx.lock(plan.lock_plan)
                        common = {
                            "tenant": tenant,
                            "reason": payload.reason,
                            "correlation_id": str(uuid4()),
                        }
                        if operation == "publish":
                            snapshot = await policies.publish(
                                db,
                                tx,
                                plan,
                                grants=grants,
                                expected_version=payload.expected_version,
                                **common,
                            )
                        else:
                            method = (
                                policies.mark_ready
                                if operation == "ready"
                                else policies.transition
                            )
                            await method(
                                db,
                                tx,
                                plan,
                                expected_epoch=payload.expected_epoch,
                                expected_manifest_digest=payload.expected_manifest_digest,
                                **common,
                            )
                        result = {
                            "policy": policy_wire(snapshot),
                            "state": state_wire(
                                await policies._state(db, tenant), blockers
                            ),
                        }
                    else:
                        result = {
                            **policy_catalog(),
                            "policy": policy_wire(snapshot),
                            "state": state_wire(state, blockers),
                        }
        except BaseException:
            await rollback_authorization_session(db)
            raise
        if writing:
            await commit_authorization_session(db)
        else:
            await rollback_authorization_session(db)
        return result
