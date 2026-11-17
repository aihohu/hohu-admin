"""Only the stopped-deployment controller uses this read-only global owner."""

from app.core.authorization_plan import TransactionBudget
from app.core.field_policy.qualification import read_server_qualification
from app.core.rbac import is_super_admin
from app.db.authorization_owner import rollback_authorization_session
from app.db.authorization_system_owner import require_candidate_session
from app.modules.auth.permission_collect import collect_user_permission_codes
from app.modules.system.service.authorization_read_budget import (
    AuthorizationReadBudget,
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions as transactions,
)
from app.modules.system.service.field_management import forbidden
from app.modules.system.service.policy_rollout_completion import capture_completion
from app.modules.system.service.user_field_context import load_current_user


class RolloutCompletionOwner:
    async def execute(self, db, *, tenant, actor_user_version, catalog):
        require_candidate_session(db)
        try:
            qualification = read_server_qualification(catalog=catalog, required=True)
            with bounded_authorization_reads(
                db, AuthorizationReadBudget(max_bytes=128 * 1024 * 1024)
            ):
                async with transactions.scope_global(
                    db, budget=TransactionBudget(timeout_ms=60000)
                ) as tx:
                    actor = await load_current_user(
                        db, tenant=tenant, actor_user_version=actor_user_version
                    )
                    if (
                        not is_super_admin(actor)
                        or "system:role:field-policy-activate"
                        not in collect_user_permission_codes(actor)
                    ):
                        raise forbidden()
                    result = await capture_completion(
                        db, tx, catalog=catalog, qualification=qualification
                    )
        except BaseException:
            await rollback_authorization_session(db)
            raise
        await rollback_authorization_session(db)
        return result
