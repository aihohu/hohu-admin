"""Test seeds retain real scale and isolation without redundant ORM graphs."""

import pytest
from sqlalchemy import event, func, select, text

from app.db.base import role_menus, user_depts, user_roles
from app.modules.system.models.menu import Menu
from app.modules.system.models.user import User
from tests.modules.system import test_user_export as export_helper
from tests.modules.system.test_authorization_system_candidate import system_sessions
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


async def test_export_threshold_keeps_full_size_without_bulk_orm_graph(
    db_session, monkeypatch
):
    original = export_helper._make_user
    seen = []

    def bounded(*args, **kwargs):
        seen.append(args[0])
        assert len(seen) <= 1, "Threshold rows must not construct an ORM graph"
        return original(*args, **kwargs)

    monkeypatch.setattr(export_helper, "_make_user", bounded)
    await export_helper.TestExportThreshold().test_export_over_threshold_raises_async_required(
        db_session, export_helper.MockFileStorage()
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.tenant_id == export_helper.TENANT.tenant_id,
                User.user_name.startswith("QA_EXP_BULK_"),
            )
        )
        == export_helper.USER_EXPORT_ASYNC_THRESHOLD + 1
    )


async def test_bulk_export_seed_matches_orm_and_savepoint_rollback(db_session):
    role = await export_helper._fetch_super_role(db_session)
    dept = export_helper._make_dept(978000, "bulk seed equivalence")
    db_session.add(dept)
    await db_session.flush()
    role_id, dept_id = role.role_id, dept.dept_id
    columns = list(User.__table__.columns)
    snapshots = []
    for bulk in (False, True):
        savepoint = await db_session.begin_nested()
        ids = (978001, 978002, 978003)
        try:
            if bulk:
                repeated = []

                def record_batch(
                    _cursor, _sql, parameters, _context, repeated=repeated
                ):
                    repeated.append(len(parameters))

                engine = db_session.bind.engine.sync_engine
                event.listen(engine, "do_executemany", record_batch)
                try:
                    await export_helper._seed_bulk_users(
                        db_session,
                        first_id=ids[0],
                        prefix="QA_EQ_",
                        width=4,
                        role_id=role_id,
                        dept_id=dept_id,
                        count=len(ids),
                    )
                finally:
                    event.remove(engine, "do_executemany", record_batch)
                assert repeated == [], (
                    "Use batched SQL rather than row-wise driver execution"
                )
            else:
                db_session.add_all(
                    [
                        export_helper._make_user(uid, f"QA_EQ_{i:04d}", [role], [dept])
                        for i, uid in enumerate(ids)
                    ]
                )
                await db_session.flush()
            users = (
                await db_session.execute(
                    select(*columns).where(User.user_id.in_(ids)).order_by(User.user_id)
                )
            ).all()
            roles = (
                await db_session.execute(
                    select(user_roles)
                    .where(user_roles.c.user_id.in_(ids))
                    .order_by(user_roles.c.user_id)
                )
            ).all()
            depts = (
                await db_session.execute(
                    select(user_depts)
                    .where(user_depts.c.user_id.in_(ids))
                    .order_by(user_depts.c.user_id)
                )
            ).all()
            snapshots.append((users, roles, depts))
            assert len(users) == len(roles) == len(depts) == 3
        finally:
            await savepoint.rollback()
        assert (
            await db_session.scalar(
                select(func.count()).select_from(User).where(User.user_id.in_(ids))
            )
            == 0
        )
        for table in (user_roles, user_depts):
            assert (
                await db_session.scalar(
                    select(func.count())
                    .select_from(table)
                    .where(table.c.user_id.in_(ids))
                )
                == 0
            )
    assert snapshots[0] == snapshots[1]


async def test_system_permission_seed_batches_without_changing_grants(plan_sessions):
    _, plain = plan_sessions
    inserts = []
    repeated = []

    def record_batch(_cursor, _sql, parameters, _context):
        repeated.append(len(parameters))

    def record(_conn, _cursor, _sql, _parameters, context, _many):
        statement = context.compiled.statement if context.compiled else None
        table = getattr(getattr(statement, "table", None), "name", None)
        if getattr(statement, "is_insert", False) and table in {
            "sys_menu",
            "sys_role_menu",
        }:
            inserts.append(table)

    engine = plain.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", record)
    event.listen(engine, "do_executemany", record_batch)
    try:
        await system_sessions.__wrapped__(plan_sessions)
    finally:
        event.remove(engine, "before_cursor_execute", record)
        event.remove(engine, "do_executemany", record_batch)
    assert inserts == ["sys_menu", "sys_role_menu"]
    assert repeated == []
    from app.modules.system.service.authorization_system_candidate import (  # noqa: PLC0415
        SYSTEM_CANDIDATE_ENTRIES,
    )

    expected = sorted(
        {p for entry in SYSTEM_CANDIDATE_ENTRIES.values() for p in entry.permissions}
    )
    async with plain() as db:
        actual = (
            await db.execute(
                select(Menu.menu_id, Menu.permission, role_menus.c.role_id)
                .join(
                    role_menus,
                    (role_menus.c.menu_id == Menu.menu_id)
                    & (role_menus.c.tenant_id == Menu.tenant_id),
                )
                .where(Menu.tenant_id == 0)
                .order_by(Menu.menu_id)
            )
        ).all()
    assert actual == [
        (index, permission, 201) for index, permission in enumerate(expected, 1001)
    ]


@pytest.mark.parametrize("failure_table", ["sys_user_role", "sys_user_dept"])
async def test_bulk_export_seed_failure_rolls_back_partial_rows(
    db_session, monkeypatch, failure_table
):
    role = await export_helper._fetch_super_role(db_session)
    dept = export_helper._make_dept(978100, "bulk seed failure")
    db_session.add(dept)
    await db_session.flush()
    role_id, dept_id = role.role_id, dept.dept_id
    original = db_session.execute

    async def fail_relation(statement, *args, **kwargs):
        if getattr(getattr(statement, "table", None), "name", None) == failure_table:
            raise RuntimeError("injected relation seed failure")
        return await original(statement, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(db_session, "execute", fail_relation)
        with pytest.raises(RuntimeError, match="injected relation seed failure"):
            async with db_session.begin_nested():
                await export_helper._seed_bulk_users(
                    db_session,
                    first_id=978101,
                    prefix="QA_PARTIAL_",
                    width=4,
                    role_id=role_id,
                    dept_id=dept_id,
                    count=3,
                )
    ids = (978101, 978102, 978103)
    for table in (User.__table__, user_roles, user_depts):
        assert (
            await db_session.scalar(
                select(func.count()).select_from(table).where(table.c.user_id.in_(ids))
            )
            == 0
        )
    assert (
        await db_session.scalar(
            select(export_helper.Dept.dept_id).where(
                export_helper.Dept.dept_id == dept_id
            )
        )
        == dept_id
    )


async def test_bulk_seed_refreshes_estimates_for_transaction_local_growth(db_session):
    """Autovacuum cannot analyze the thousands of uncommitted fixture rows."""
    role = await export_helper._fetch_super_role(db_session)
    dept = export_helper._make_dept(979000, "bulk seed planner estimates")
    db_session.add(dept)
    await db_session.flush()
    await db_session.execute(text("ANALYZE sys_user, sys_user_role, sys_user_dept"))
    await export_helper._seed_bulk_users(
        db_session,
        first_id=980000,
        prefix="QA_EST_",
        width=5,
        role_id=role.role_id,
        dept_id=dept.dept_id,
    )
    for query in (
        "EXPLAIN (FORMAT JSON) SELECT user_id FROM sys_user WHERE tenant_id = 0",
        "EXPLAIN (FORMAT JSON) SELECT user_id FROM sys_user_role WHERE tenant_id = 0",
        "EXPLAIN (FORMAT JSON) SELECT user_id FROM sys_user_dept WHERE tenant_id = 0",
    ):
        plan = await db_session.scalar(text(query))
        assert (
            plan[0]["Plan"]["Plan Rows"] >= export_helper.USER_EXPORT_ASYNC_THRESHOLD
        ), "Planner estimates must include transaction-local bulk fixture rows"
