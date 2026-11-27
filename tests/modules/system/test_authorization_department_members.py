"""A department's final membership is one planned authorization mutation."""

import pytest
from sqlalchemy import insert, select, update

from app.core.exceptions import BusinessException
from app.db.authorization_runtime import AuthorizationRuntime
from app.db.base import user_depts, user_roles
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from tests.modules.system.test_authorization_system_candidate import TENANT, epoch
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


@pytest.fixture
async def members(system_sessions):
    _, plain = system_sessions
    async with plain() as db:
        await db.execute(
            insert(Dept).values(
                tenant_id=0, dept_id=401, dept_name="Team", ancestors="0", status="1"
            )
        )
        await db.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=301,
                role_name="Member",
                role_code="R_USER",
                data_scope="5",
                status="1",
            )
        )
        for uid in (501, 502, 503):
            await db.execute(
                insert(User).values(
                    tenant_id=0,
                    user_id=uid,
                    user_name=f"member-{uid}",
                    hashed_password="unused",
                    status="1",
                )
            )
            await db.execute(
                insert(user_roles).values(tenant_id=0, user_id=uid, role_id=301)
            )
        await db.execute(
            insert(user_depts).values(
                tenant_id=0, user_id=501, dept_id=401, is_primary="N"
            )
        )
        await db.commit()
    return AuthorizationRuntime(plain.kw["bind"]), plain


async def replace(runtime, ids):
    return await runtime.system(
        tenant=TENANT,
        actor_user_version=1,
        operation="dept.members",
        target_id=401,
        payload=ids,
    )


async def membership(db):
    return list(
        (
            await db.execute(
                select(user_depts.c.user_id, user_depts.c.is_primary)
                .where(user_depts.c.tenant_id == 0, user_depts.c.dept_id == 401)
                .order_by(user_depts.c.user_id)
            )
        ).all()
    )


async def test_complete_replacement_advances_one_epoch_and_noop_does_not(members):
    runtime, plain = members
    result = await replace(runtime, ["502", "503"])
    assert (result["result"].added, result["result"].removed) == (2, 1)
    async with plain() as db:
        assert await membership(db) == [(502, "N"), (503, "N")]
        assert await epoch(db) == 2
    result = await replace(runtime, ["503", "502"])
    assert (result["result"].added, result["result"].removed) == (0, 0)
    async with plain() as db:
        assert await epoch(db) == 2


@pytest.mark.parametrize(
    "case,code",
    [
        ("primary", "USER_PRIMARY_DEPT_REASSIGN_REQUIRED"),
        ("disabled-user", "USER_DEPT_MEMBER_NOT_AVAILABLE"),
        ("disabled-dept", "USER_DEPT_NOT_AVAILABLE"),
        ("foreign-user", "USER_DEPT_MEMBER_NOT_AVAILABLE"),
        ("duplicate", "USER_DEPT_MEMBER_NOT_AVAILABLE"),
        ("malformed", "USER_DEPT_MEMBER_NOT_AVAILABLE"),
        ("limit", "AUTHORIZATION_PLAN_LIMIT_EXCEEDED"),
    ],
)
async def test_rejected_member_set_has_no_partial_effects(members, case, code):
    runtime, plain = members
    ids = ["502", "503"]
    async with plain() as db:
        if case == "primary":
            await db.execute(
                update(user_depts)
                .where(user_depts.c.user_id == 501)
                .values(is_primary="Y")
            )
        elif case == "disabled-user":
            await db.execute(update(User).where(User.user_id == 503).values(status="2"))
        elif case == "disabled-dept":
            await db.execute(update(Dept).where(Dept.dept_id == 401).values(status="2"))
        elif case == "foreign-user":
            ids = ["502", "102"]
        elif case == "duplicate":
            ids = ["502", 502]
        elif case == "malformed":
            ids = ["502", "0503"]
        elif case == "limit":
            ids = list(range(1, 258))
        await db.commit()
        before = await membership(db)
    with pytest.raises(BusinessException) as caught:
        await replace(runtime, ids)
    assert caught.value.error_code == code
    async with plain() as db:
        assert await membership(db) == before
        assert await epoch(db) == 1
