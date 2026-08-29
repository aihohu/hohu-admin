"""Regression tests for idempotent data-scope demo user seeding."""

from sqlalchemy import select

from app.constants import STATUS_ENABLED
from app.core.id_generator import next_id
from app.db.base import user_depts, user_roles
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from scripts import seed_demo_data_scope


async def test_partial_legacy_seed_is_reconciled_without_skipping_missing_users(
    db_session, monkeypatch
) -> None:
    dept_id = next_id()
    role_id = next_id()
    existing_user_id = next_id()
    missing_user_id = next_id()
    old_name = f"old_{str(existing_user_id)[-6:]}"
    new_name = f"old{str(existing_user_id)[-6:]}"
    missing_name = f"new{str(missing_user_id)[-6:]}"
    dept = Dept(
        dept_id=dept_id,
        dept_name=f"demo-{dept_id}",
        ancestors="0",
        order_num=1,
        status=STATUS_ENABLED,
    )
    role = Role(
        role_id=role_id,
        role_name=f"demo-{role_id}",
        role_code=f"R_DEMO_{role_id}",
        data_scope="1",
        status=STATUS_ENABLED,
    )
    existing = User(
        user_id=existing_user_id,
        user_name=old_name,
        nickname="existing",
        hashed_password="hash",
        status=STATUS_ENABLED,
    )
    db_session.add_all([dept, role, existing])
    await db_session.flush()
    monkeypatch.setattr(
        seed_demo_data_scope,
        "USERS",
        [
            (existing_user_id, new_name, "existing", role_id, dept_id, [dept_id]),
            (missing_user_id, missing_name, "missing", role_id, dept_id, [dept_id]),
        ],
    )
    monkeypatch.setattr(
        seed_demo_data_scope,
        "LEGACY_USER_NAMES",
        {existing_user_id: old_name},
    )

    await seed_demo_data_scope._seed_users(db_session)

    users = (
        (
            await db_session.execute(
                select(User).where(
                    User.user_id.in_([existing_user_id, missing_user_id])
                )
            )
        )
        .scalars()
        .all()
    )
    assert {user.user_name for user in users} == {new_name, missing_name}
    assert set(
        await db_session.scalars(
            select(user_roles.c.user_id).where(
                user_roles.c.user_id.in_([existing_user_id, missing_user_id])
            )
        )
    ) == {existing_user_id, missing_user_id}
    assert set(
        await db_session.scalars(
            select(user_depts.c.user_id).where(
                user_depts.c.user_id.in_([existing_user_id, missing_user_id])
            )
        )
    ) == {existing_user_id, missing_user_id}
