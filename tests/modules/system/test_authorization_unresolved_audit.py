"""An unresolved login reservation cannot impersonate a tenant audit row."""

import pytest

from app.core.authorization_plan import LockPlan, NewResource, ResourceKey, ResourceKind
from app.core.exceptions import BusinessException
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from tests.modules.system.test_authorization_system_candidate import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)


@pytest.mark.parametrize(
    "change",
    [{"tenant_id": 0}, {"user_id": 101}, {"status": "1"}, {"audit_scope": "tenant"}],
)
async def test_unresolved_audit_rejects_attributed_or_success_facts(
    system_sessions, change
):
    factory, _ = system_sessions
    key = ResourceKey(ResourceKind.UNRESOLVED_LOGIN_LOG, None, 902)
    values = {
        "tenant_id": None,
        "user_id": None,
        "username": "unknown",
        "status": "2",
        "audit_scope": "unresolved",
    } | change
    async with factory() as db:
        with pytest.raises(BusinessException):
            async with authorization_transactions.scope(db) as tx:
                await tx.lock(LockPlan(new=(NewResource(key),)))
                await tx.append_audit(key, values)


@pytest.mark.parametrize("operation", ["lock_existing", "ordinary_insert"])
async def test_unresolved_audit_is_only_a_reserved_append(system_sessions, operation):
    factory, _ = system_sessions
    key = ResourceKey(ResourceKind.UNRESOLVED_LOGIN_LOG, None, 902)
    async with factory() as db:
        with pytest.raises(BusinessException):
            async with authorization_transactions.scope(db) as tx:
                if operation == "lock_existing":
                    await tx.lock(LockPlan(existing=(key,)))
                else:
                    await tx.lock(LockPlan(new=(NewResource(key),)))
                    await tx.insert_reserved(
                        key,
                        {
                            "username": "unknown",
                            "status": "2",
                            "audit_scope": "unresolved",
                        },
                    )
