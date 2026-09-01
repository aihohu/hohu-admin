"""Real PostgreSQL constraints reject cross-tenant relationships."""

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from app.core.id_generator import next_id
from app.db.base import user_roles
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from tests.tenant_helpers import create_test_tenant


async def test_composite_foreign_keys_reject_cross_tenant_links(db_session):
    tenant_b = await create_test_tenant(db_session, prefix="constraint-b")
    user_a = User(
        tenant_id=0,
        user_name=f"constraint-user-{next_id()}",
        nickname="A",
        hashed_password="x",
        status="1",
    )
    role_b = Role(
        tenant_id=tenant_b.tenant_id,
        role_name=f"Constraint role {next_id()}",
        role_code=f"R_CONSTRAINT_{next_id()}",
        data_scope="1",
        status="1",
    )
    parent_b = Dept(
        tenant_id=tenant_b.tenant_id,
        dept_name=f"Constraint parent {next_id()}",
        ancestors="0",
        order_num=1,
        status="1",
    )
    db_session.add_all([user_a, role_b, parent_b])
    await db_session.flush()

    link_savepoint = await db_session.begin_nested()
    with pytest.raises(IntegrityError):
        await db_session.execute(
            insert(user_roles).values(
                tenant_id=0,
                user_id=user_a.user_id,
                role_id=role_b.role_id,
            )
        )
    await link_savepoint.rollback()

    child_savepoint = await db_session.begin_nested()
    child_a = Dept(
        tenant_id=0,
        dept_name=f"Constraint child {next_id()}",
        parent_id=parent_b.dept_id,
        ancestors=f"0,{parent_b.dept_id}",
        order_num=1,
        status="1",
    )
    db_session.add(child_a)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await child_savepoint.rollback()

    count = await db_session.scalar(
        select(func.count())
        .select_from(user_roles)
        .where(
            user_roles.c.tenant_id == 0,
            user_roles.c.user_id == user_a.user_id,
            user_roles.c.role_id == role_b.role_id,
        )
    )
    assert count == 0
