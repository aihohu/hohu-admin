"""Department audit fields must come from the locked actor, before commit."""

import pytest
from sqlalchemy import select

from app.db.authorization_runtime import AuthorizationRuntime
from app.modules.system.models.dept import Dept
from app.modules.system.models.user import User
from app.modules.system.schemas.dept import DeptCreate, DeptMove, DeptUpdate
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


@pytest.mark.parametrize("operation", ["dept.create", "dept.update", "dept.move"])
async def test_department_provenance_is_in_the_same_commit(system_sessions, operation):
    _, plain = system_sessions
    runtime = AuthorizationRuntime(plain.kw["bind"])
    created = await runtime.system(
        tenant=TENANT,
        actor_user_version=1,
        operation="dept.create",
        payload=DeptCreate(dept_name="Original", status="1"),
    )
    identifier = created["id"]
    if operation == "dept.update":
        await runtime.system(
            tenant=TENANT,
            actor_user_version=1,
            operation=operation,
            target_id=identifier,
            payload=DeptUpdate(dept_name="Changed"),
        )
    elif operation == "dept.move":
        parent = await runtime.system(
            tenant=TENANT,
            actor_user_version=1,
            operation="dept.create",
            payload=DeptCreate(dept_name="Parent", status="1"),
        )
        await runtime.system(
            tenant=TENANT,
            actor_user_version=1,
            operation=operation,
            target_id=identifier,
            payload=DeptMove(new_parent_id=str(parent["id"])),
        )
    async with plain() as db:
        actor = await db.scalar(
            select(User.user_name).where(User.user_id == TENANT.actor_user_id)
        )
        dept = await db.get(Dept, identifier)
        assert dept.create_by == actor
        if operation != "dept.create":
            assert dept.update_by == actor
