"""A strict, rollback-only completion boundary for relation reads."""

from app.core.authorization_plan import LockMode, TenantLock, TransactionBudget
from app.db.authorization_owner import rollback_authorization_session
from app.db.authorization_plan_session import _order_error
from app.db.authorization_system_owner import require_candidate_session
from app.modules.system.schemas.relation_form import serialize_form
from app.modules.system.schemas.relation_projection import serialize_relation
from app.modules.system.service.authorization_read_budget import (
    RELATION_READ_PHASE_LIMITS,
    AuthorizationReadBudget,
    authorization_read_phase,
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.relation_read_service import read_relation
from app.modules.system.service.runtime_navigation import runtime_routes


class RelationReadOwner:
    async def execute(
        self, db, *, tenant, actor_user_version, operation, query=None, target_id=None
    ):
        require_candidate_session(db)
        if operation == "runtime.routes" and (
            query is not None or target_id is not None
        ):
            raise _order_error()
        if operation == "runtime.exists" and (
            type(query) is not str
            or not 1 <= len(query) <= 100
            or target_id is not None
        ):
            raise _order_error()
        try:
            with bounded_authorization_reads(
                db,
                AuthorizationReadBudget(
                    max_statements=4096, phase_limits=RELATION_READ_PHASE_LIMITS
                ),
            ):
                async with authorization_transactions.scope(
                    db,
                    tenants=(TenantLock(tenant.tenant_id, LockMode.SHARED),),
                    budget=TransactionBudget(timeout_ms=8000),
                ) as tx:
                    with authorization_read_phase(db, "projection"):
                        if operation in {"runtime.routes", "runtime.exists"}:
                            navigation = await runtime_routes(
                                db,
                                tx,
                                tenant=tenant,
                                actor_user_version=actor_user_version,
                            )
                            if operation == "runtime.routes":
                                return navigation
                            pending = list(navigation["routes"])
                            while pending:
                                route = pending.pop()
                                if route["name"] == query:
                                    return True
                                pending.extend(route.get("children", ()))
                            return False
                        result = await read_relation(
                            db,
                            tx,
                            tenant=tenant,
                            actor_user_version=actor_user_version,
                            operation=operation,
                            query=query,
                            target_id=target_id,
                        )
                        if operation.endswith(".form"):
                            return serialize_form(operation.split(".")[0], result)
                        return serialize_relation(operation, result)
        finally:
            await rollback_authorization_session(db)


relation_read_owner = RelationReadOwner()
