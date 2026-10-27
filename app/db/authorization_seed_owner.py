"""Explicit offline seed owner; legacy scripts are never run inside a proof."""

from app.core.authorization_io import hash_password
from app.core.authorization_maintenance import require_maintenance
from app.core.authorization_plan import LockPlan, TransactionBudget
from app.core.authorization_seed_plan import SeedPlan
from app.db.authorization_control_owner import _rollback_preserving
from app.db.authorization_owner import commit_authorization_session
from app.db.authorization_plan_session import _order_error
from app.db.authorization_system_owner import require_candidate_session
from app.modules.ai.service.authorization_seed_candidate import ai_seed_candidate
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_seed_candidate import (
    system_seed_candidate,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.utils.validators import validate_password


class SeedCandidateOwner:
    async def execute(self, db, *, action, fence, admin_password=None):
        require_candidate_session(db)
        if action not in {"seed.system", "seed.agents", "seed.settings"}:
            raise _order_error()
        evidence = require_maintenance(fence, action)
        password_hash = None
        if admin_password is not None:
            validate_password(admin_password)
            password_hash = await hash_password(admin_password)
        try:
            require_maintenance(fence, action)
            with bounded_authorization_reads(db):
                async with authorization_transactions.scope_global(
                    db, budget=TransactionBudget(timeout_ms=60000)
                ) as tx:
                    ai, published = (
                        await ai_seed_candidate.plan(db)
                        if action != "seed.settings"
                        else (SeedPlan((), ()), ())
                    )
                    system, fresh = (
                        await system_seed_candidate.plan(
                            db,
                            action=action,
                            password_hash=password_hash,
                            published_agents=published,
                        )
                        if action != "seed.agents"
                        else (SeedPlan((), ()), False)
                    )
                    await tx.lock(
                        LockPlan(
                            existing=tuple(set(ai.existing + system.existing)),
                            new=ai.new + system.new,
                        )
                    )
                    async with tx.aggregate_global():
                        await ai_seed_candidate.apply(tx, ai)
                        await system_seed_candidate.apply(tx, system)
                    result = {
                        "action": action,
                        "fresh": fresh,
                        "planned_changes": len(ai.changes) + len(system.changes),
                        "maintenance_manifest": evidence.manifest_digest,
                    }
        except BaseException as error:
            await _rollback_preserving(db, error)
            raise
        await commit_authorization_session(db)
        return result


seed_candidate_owner = SeedCandidateOwner()
