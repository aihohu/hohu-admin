"""Authorization INSERT cannot escape the aggregate epoch ledger."""

import pytest
from sqlalchemy import insert

from app.core.authorization_plan import LockPlan, NewResource, ResourceKey, ResourceKind
from app.core.exceptions import BusinessException
from app.modules.system.models.user_field_policy import UserFieldPolicyRevision
from tests.modules.system.test_authorization_transaction_mutations import (
    ROLE,
    USER,
    scope,
)
from tests.modules.system.test_authorization_transaction_mutations import (
    mutation_sessions as mutation_sessions,
)


@pytest.mark.parametrize(
    ("kind", "identity", "values"),
    [
        (
            ResourceKind.USER,
            102,
            {"user_name": "unaggregated", "hashed_password": "fixture", "status": "1"},
        ),
        (
            ResourceKind.ROLE,
            202,
            {"role_name": "Unaggregated", "role_code": "UNAGGREGATED", "status": "1"},
        ),
        (
            ResourceKind.DEPT,
            301,
            {
                "dept_name": "Unaggregated",
                "parent_id": 0,
                "ancestors": "0",
                "status": "1",
            },
        ),
        (ResourceKind.USER_ROLES, (101, 201), {}),
    ],
)
async def test_authorization_insert_without_aggregate_rejected(
    mutation_sessions, kind, identity, values
):
    managed, _ = mutation_sessions
    key = ResourceKey(kind, 0, identity)
    async with managed() as db:
        with pytest.raises(BusinessException) as error:
            async with scope(db) as tx:
                await tx.lock(LockPlan(existing=(ROLE, USER), new=(NewResource(key),)))
                await tx.insert_reserved(key, values)
        assert error.value.error_code == "FIELD_POLICY_LOCK_ORDER"


@pytest.mark.parametrize(
    "kind",
    [
        ResourceKind.POLICY_REVISION,
        ResourceKind.POLICY_BINDING,
        ResourceKind.POLICY_AUDIT,
    ],
)
async def test_policy_insert_without_policy_frame_rejected(mutation_sessions, kind):
    managed, plain = mutation_sessions
    revision = {
        "tenant_id": 0,
        "role_id": 201,
        "revision_no": 1,
        "catalog_revision": "fixture",
        "catalog_digest": "a" * 64,
        "rules": [],
        "rules_digest": "b" * 64,
        "created_by_user_id": 101,
    }
    if kind is ResourceKind.POLICY_BINDING:
        async with plain() as db:
            await db.execute(
                insert(UserFieldPolicyRevision).values(revision_id=401, **revision)
            )
            await db.commit()
        key = ResourceKey(kind, 0, 201)
        values = {"revision_id": 401, "version": 1}
    elif kind is ResourceKind.POLICY_REVISION:
        key, values = ResourceKey(kind, 0, 401), revision
    else:
        key = ResourceKey(kind, 0, 501)
        values = {
            "actor_user_id": 101,
            "action": "ready",
            "reason": "fixture",
            "correlation_id": "fixture",
            "after_digest": "a" * 64,
            "before_version": 0,
            "after_version": 1,
            "epoch": 2,
        }
    async with managed() as db:
        with pytest.raises(BusinessException) as error:
            async with scope(db) as tx:
                await tx.lock(LockPlan(existing=(ROLE, USER), new=(NewResource(key),)))
                await tx.insert_reserved(key, values)
        assert error.value.error_code == "FIELD_POLICY_LOCK_ORDER"
