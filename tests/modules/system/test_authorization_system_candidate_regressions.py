"""Independent real-DB business regressions for the explicit System candidate."""

import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, event, insert, select, update

from app.core.authorization_io import hash_password, verify_password
from app.core.authorization_plan import LockMode, ResourceKind, TenantLock
from app.core.exceptions import BusinessException
from app.db.base import Base, role_depts, role_menus, user_depts, user_roles
from app.modules.marketplace.models import App, AppRating, AppVersion
from app.modules.system.field_policy.evaluator import FieldGrant
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import UserFieldPolicyGlobalGuard
from app.modules.system.schemas.dept import DeptMove, DeptUpdate
from app.modules.system.schemas.role import RoleUpdate
from app.modules.system.schemas.user import (
    ChangePassword,
    ResetPassword,
    UpdateProfile,
    UserUpdate,
)
from app.modules.system.service.authorization_resource_catalog import resource_spec
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions as transactions,
)
from app.modules.system.service.planned_user_field_policy_service import (
    planned_user_field_policy_service as policies,
)
from tests.modules.system.test_authorization_system_candidate import TENANT, epoch, run
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


@pytest.fixture
async def populated(system_sessions):
    _, plain = system_sessions
    async with plain.kw["bind"].begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[App.__table__, AppVersion.__table__, AppRating.__table__]
            )
        )
    async with plain() as db:
        for rid in (203, 204, 205, 206):
            await db.execute(
                insert(Role).values(
                    tenant_id=0,
                    role_id=rid,
                    role_code=f"R_{rid}",
                    role_name=f"role-{rid}",
                    status="1",
                    data_scope="1",
                    i18n_keys={"roleName": "old.name", "roleDesc": "old.desc"},
                )
            )
        for uid, rid in ((103, 203), (104, 204)):
            await db.execute(
                insert(User).values(
                    tenant_id=0,
                    user_id=uid,
                    user_name=f"user{uid}",
                    hashed_password="not-a-credential",
                    status="1",
                    user_email=f"user{uid}@example.com",
                    email_verified_at=datetime.now(UTC),
                )
            )
            await db.execute(
                insert(user_roles).values(tenant_id=0, user_id=uid, role_id=rid)
            )
        for did, parent, ancestors in (
            (401, None, "0"),
            (402, 401, "0,401"),
            (403, 402, "0,401,402"),
            (404, None, "0"),
            (405, None, "0"),
        ):
            await db.execute(
                insert(Dept).values(
                    tenant_id=0,
                    dept_id=did,
                    dept_name=f"dept-{did}",
                    parent_id=parent,
                    ancestors=ancestors,
                    status="1",
                )
            )
        await db.execute(
            insert(user_depts).values(
                tenant_id=0,
                user_id=103,
                dept_id=402,
                is_primary="Y",
            )
        )
        await db.commit()
    return system_sessions


async def apply(
    factory, operation, payload=None, target_id=None, *, commit=True, **kwargs
):
    async with factory() as db:
        result = await run(db, operation, payload, target_id, **kwargs)
        if commit:
            await db.commit()
        else:
            await db.rollback()
        return result


async def read_epoch(plain):
    async with plain() as db:
        return await epoch(db)


async def rows(plain, statement):
    async with plain() as db:
        return (await db.execute(statement)).all()


async def test_role_menu_replace_noop_and_stale_snapshot(populated):
    factory, plain = populated
    menu_ids = [
        row[0] for row in await rows(plain, select(Menu.menu_id).order_by(Menu.menu_id))
    ][:2]
    await apply(factory, "role.menus", menu_ids, 205)
    assert await rows(
        plain,
        select(role_menus.c.menu_id)
        .where(role_menus.c.role_id == 205)
        .order_by(role_menus.c.menu_id),
    ) == [(mid,) for mid in menu_ids]
    assert await read_epoch(plain) == 2
    await apply(factory, "role.menus", list(reversed(menu_ids)), 205)
    assert await read_epoch(plain) == 2
    with pytest.raises(BusinessException) as failure:
        await apply(
            factory, "role.menus", menu_ids[:1], 205, expected_snapshot={"stale": True}
        )
    assert failure.value.error_code == "AI_PREPARED_ACTION_SNAPSHOT_STALE"
    assert await read_epoch(plain) == 2
    await apply(factory, "role.menus", menu_ids[:1], 205)
    assert await read_epoch(plain) == 3
    assert await rows(
        plain, select(role_menus.c.menu_id).where(role_menus.c.role_id == 205)
    ) == [(menu_ids[0],)]


async def test_role_scope_departments_and_translation_invalidation(populated):
    factory, plain = populated
    await apply(
        factory,
        "role.update",
        RoleUpdate(role_name="changed", data_scope="2", dept_ids=[401, 402]),
        205,
    )
    assert await rows(
        plain,
        select(role_depts.c.dept_id)
        .where(role_depts.c.role_id == 205)
        .order_by(role_depts.c.dept_id),
    ) == [(401,), (402,)]
    assert await rows(plain, select(Role.i18n_keys).where(Role.role_id == 205)) == [
        ({"roleDesc": "old.desc"},)
    ]
    await apply(factory, "role.update", RoleUpdate(data_scope="1"), 205)
    assert (
        await rows(
            plain, select(role_depts.c.dept_id).where(role_depts.c.role_id == 205)
        )
        == []
    )
    assert await read_epoch(plain) == 3


async def test_department_subtree_move_noop_and_cycle_rejection(populated):
    factory, plain = populated
    await apply(factory, "dept.move", DeptMove(new_parent_id="404"), 401)
    query = (
        select(Dept.dept_id, Dept.parent_id, Dept.ancestors)
        .where(Dept.dept_id.in_([401, 402, 403]))
        .order_by(Dept.dept_id)
    )
    expected = [
        (401, 404, "0,404"),
        (402, 401, "0,404,401"),
        (403, 402, "0,404,401,402"),
    ]
    assert await rows(plain, query) == expected
    assert await read_epoch(plain) == 2
    with pytest.raises(BusinessException) as unchanged:
        await apply(factory, "dept.move", DeptMove(new_parent_id="404"), 401)
    assert unchanged.value.error_code == "AI_DEPT_MOVE_UNCHANGED"
    assert await read_epoch(plain) == 2
    with pytest.raises(BusinessException):
        await apply(factory, "dept.move", DeptMove(new_parent_id="403"), 401)
    assert await rows(plain, query) == expected
    assert await read_epoch(plain) == 2


async def test_department_move_back_to_root_preserves_explicit_null(populated):
    factory, plain = populated
    await apply(factory, "dept.move", DeptMove(new_parent_id="404"), 401)
    await apply(factory, "dept.move", DeptMove(new_parent_id=None), 401)
    assert await rows(
        plain,
        select(Dept.dept_id, Dept.parent_id, Dept.ancestors)
        .where(Dept.dept_id.in_([401, 402, 403]))
        .order_by(Dept.dept_id),
    ) == [(401, None, "0"), (402, 401, "0,401"), (403, 402, "0,401,402")]
    assert await read_epoch(plain) == 3


@pytest.mark.parametrize("invalid", [True, 404.0, "0404", -1])
async def test_department_move_revalidates_tampered_model_before_database(
    populated, invalid
):
    factory, plain = populated
    payload = DeptMove(new_parent_id="404")
    payload.new_parent_id = invalid
    async with factory() as db:
        with pytest.raises(ValidationError):
            await run(db, "dept.move", payload, 401)
        assert not db.in_transaction()
        assert db._authorization_journal.phase == "idle"
    assert await read_epoch(plain) == 1


async def test_department_move_freezes_payload_before_first_barrier_wait(populated):
    factory, plain = populated
    payload = DeptMove(new_parent_id="404")
    waiting = asyncio.Event()
    candidate = None

    def observe(_connection, _cursor, statement, _params, _context, _many):
        if (
            asyncio.current_task() is candidate
            and "sys_user_field_policy_global_guard" in statement
            and "FOR SHARE" in statement
        ):
            waiting.set()

    engine = plain.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", observe)
    try:
        async with plain() as blocker:
            await blocker.scalar(
                select(UserFieldPolicyGlobalGuard.guard_id)
                .where(UserFieldPolicyGlobalGuard.guard_id == 1)
                .with_for_update()
            )
            candidate = asyncio.create_task(apply(factory, "dept.move", payload, 401))
            async with asyncio.timeout(8):
                await waiting.wait()
                assert not candidate.done()
                payload.new_parent_id = 405
                await blocker.rollback()
                await candidate
        assert payload.new_parent_id == 405
        assert await rows(
            plain, select(Dept.parent_id, Dept.ancestors).where(Dept.dept_id == 401)
        ) == [(404, "0,404")]
        assert await read_epoch(plain) == 2
    finally:
        event.remove(engine, "before_cursor_execute", observe)
        if candidate is not None:
            if not candidate.done():
                candidate.cancel()
            await asyncio.gather(candidate, return_exceptions=True)


async def test_department_status_update_noop_and_owner_rollback(populated):
    factory, plain = populated
    await apply(factory, "dept.update", DeptUpdate(status="2"), 405, commit=False)
    assert await rows(plain, select(Dept.status).where(Dept.dept_id == 405)) == [("1",)]
    assert await read_epoch(plain) == 1
    await apply(factory, "dept.update", DeptUpdate(status="2"), 405)
    await apply(factory, "dept.update", DeptUpdate(status="2"), 405)
    assert await rows(plain, select(Dept.status).where(Dept.dept_id == 405)) == [("2",)]
    assert await read_epoch(plain) == 2


async def test_user_role_replace_noop_and_snapshot_rejection(populated):
    factory, plain = populated
    await apply(factory, "user.roles", [204], 103)
    assert await rows(
        plain, select(user_roles.c.role_id).where(user_roles.c.user_id == 103)
    ) == [(204,)]
    assert await read_epoch(plain) == 2
    await apply(factory, "user.roles", [204], 103)
    assert await read_epoch(plain) == 2
    with pytest.raises(BusinessException) as failure:
        await apply(
            factory, "user.roles", [203], 103, expected_snapshot={"stale": True}
        )
    assert failure.value.error_code == "AI_PREPARED_ACTION_SNAPSHOT_STALE"
    assert await read_epoch(plain) == 2


async def test_primary_department_swap_and_noop(populated):
    factory, plain = populated
    await apply(factory, "user.departments", [(401, False), (402, True)], 103)
    await apply(factory, "user.departments", [(401, True), (402, False)], 103)
    await apply(factory, "user.departments", [(402, False), (401, True)], 103)
    assert await rows(
        plain,
        select(user_depts.c.dept_id, user_depts.c.is_primary)
        .where(user_depts.c.user_id == 103)
        .order_by(user_depts.c.dept_id),
    ) == [(401, "Y"), (402, "N")]
    assert await read_epoch(plain) == 3


async def test_user_status_email_update_and_noop(populated):
    factory, plain = populated
    value = UserUpdate(
        user_name="user103", status="2", user_email="changed@example.com"
    )
    await apply(factory, "user.update", value, 103)
    await apply(factory, "user.update", value, 103)
    assert await rows(
        plain,
        select(User.status, User.auth_version, User.email_verified_at).where(
            User.user_id == 103
        ),
    ) == [("2", 2, None)]
    assert await read_epoch(plain) == 2


async def test_password_reset_revoke_and_self_change_have_separate_epochs(populated):
    factory, plain = populated
    await apply(
        factory, "user.password", ResetPassword(new_password="New-pass-234"), 103
    )
    await apply(factory, "user.revoke", target_id=103)
    assert await rows(plain, select(User.auth_version).where(User.user_id == 103)) == [
        (3,)
    ]
    stored = (
        await rows(plain, select(User.hashed_password).where(User.user_id == 103))
    )[0][0]
    assert await verify_password("New-pass-234", stored)
    hashed = await hash_password("Old-pass-123")
    async with plain() as db:
        await db.execute(
            update(User).where(User.user_id == 101).values(hashed_password=hashed)
        )
        await db.commit()
    await apply(
        factory,
        "user.change_password",
        ChangePassword(old_password="Old-pass-123", new_password="Other-pass-345"),
        104,  # Self-service must ignore an attempted alternate target.
    )
    stored, version = (
        await rows(
            plain,
            select(User.hashed_password, User.auth_version).where(User.user_id == 101),
        )
    )[0]
    assert version == 2 and await verify_password("Other-pass-345", stored)
    assert await rows(plain, select(User.auth_version).where(User.user_id == 104)) == [
        (1,)
    ]
    assert await read_epoch(plain) == 4


@pytest.mark.parametrize("commit", [False, True])
async def test_user_delete_includes_associations_and_owner_rollback(populated, commit):
    factory, plain = populated
    await apply(factory, "user.delete", target_id=103, commit=commit)
    assert await rows(plain, select(User.user_id).where(User.user_id == 103)) == (
        [] if commit else [(103,)]
    )
    assert await rows(
        plain, select(user_roles.c.user_id).where(user_roles.c.user_id == 103)
    ) == ([] if commit else [(103,)])
    assert await rows(
        plain, select(user_depts.c.user_id).where(user_depts.c.user_id == 103)
    ) == ([] if commit else [(103,)])
    assert await read_epoch(plain) == (2 if commit else 1)


async def test_user_batch_delete_preserves_actor_and_other_tenant(populated):
    factory, plain = populated
    with pytest.raises(BusinessException) as failure:
        await apply(factory, "user.batch_delete", [103, 101])
    assert failure.value.error_code == "USER_DELETE_PROTECTED"
    assert await read_epoch(plain) == 1
    result = await apply(factory, "user.batch_delete", [104, 103])
    assert result["result"] == 2
    assert await rows(plain, select(User.user_id).order_by(User.user_id)) == [
        (101,),
        (102,),
    ]
    assert await read_epoch(plain) == 2


async def attach_agent(plain, role_ids):
    agent = resource_spec(ResourceKind.AGENT).table
    relation = resource_spec(ResourceKind.ROLE_AGENT).table
    async with plain() as db:
        await db.execute(
            insert(agent).values(
                agent_id=501,
                code="delete-test",
                name="retained",
                description="test",
                enabled=True,
            )
        )
        for rid in role_ids:
            await db.execute(
                insert(relation).values(tenant_id=0, role_id=rid, agent_id=501)
            )
        await db.commit()
    return agent, relation


@pytest.mark.parametrize("batch", [False, True])
async def test_role_delete_explicitly_removes_agent_relation_but_keeps_agent(
    populated, batch
):
    factory, plain = populated
    agent, relation = await attach_agent(plain, (205, 206))
    result = await apply(
        factory,
        "role.batch_delete" if batch else "role.delete",
        [205, 206] if batch else None,
        None if batch else 205,
    )
    assert result["result"] == (2 if batch else 1)
    assert await rows(plain, select(agent.c.agent_id)) == [(501,)]
    assert await rows(plain, select(relation.c.role_id)) == ([] if batch else [(206,)])
    assert await read_epoch(plain) == 2


async def publish_history(factory, *, archived):
    for action in ("publish", "archive") if archived else ("publish",):
        async with factory() as db:
            async with transactions.scope(
                db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
            ) as tx:
                plan = await policies.plan(
                    db, tx, tenant=TENANT, action=action, role_id=205
                )
                await tx.lock(plan.lock_plan)
                kwargs = (
                    {
                        "grants": (
                            FieldGrant("205", "nickname", "read", "management.read"),
                        )
                    }
                    if action == "publish"
                    else {}
                )
                await getattr(policies, action)(
                    db,
                    tx,
                    plan,
                    tenant=TENANT,
                    expected_version=0 if action == "publish" else 1,
                    reason="history test",
                    correlation_id="history",
                    **kwargs,
                )
            await db.commit()


@pytest.mark.parametrize("archived", [False, True])
async def test_history_rejects_whole_role_batch_before_agent_or_other_delete(
    populated, archived
):
    factory, plain = populated
    _, relation = await attach_agent(plain, (205, 206))
    await publish_history(factory, archived=archived)
    before = await read_epoch(plain)
    with pytest.raises(BusinessException) as failure:
        await apply(factory, "role.batch_delete", [206, 205])
    assert failure.value.error_code == "ROLE_DELETE_POLICY_HISTORY_REFERENCED"
    assert await rows(
        plain,
        select(Role.role_id).where(Role.role_id.in_([205, 206])).order_by(Role.role_id),
    ) == [(205,), (206,)]
    assert await rows(
        plain, select(relation.c.role_id).order_by(relation.c.role_id)
    ) == [(205,), (206,)]
    assert await read_epoch(plain) == before


@pytest.mark.parametrize(
    "operation,payload,target,error",
    [
        ("role.delete", None, 203, "ROLE_DELETE_REFERENCED"),
        ("role.delete", None, 201, "ROLE_DELETE_PROTECTED"),
        ("dept.delete", None, 401, "DEPT_DELETE_REFERENCED"),
        ("dept.batch_delete", [405, 402], None, "DEPT_DELETE_REFERENCED"),
    ],
)
async def test_destructive_references_reject_before_any_mutation(
    populated, operation, payload, target, error
):
    factory, plain = populated
    with pytest.raises(BusinessException) as failure:
        await apply(factory, operation, payload, target)
    assert failure.value.error_code == error
    assert await read_epoch(plain) == 1
    assert await rows(plain, select(Dept.dept_id).where(Dept.dept_id == 405)) == [
        (405,)
    ]


@pytest.mark.parametrize("batch", [False, True])
async def test_unreferenced_department_delete(populated, batch):
    factory, plain = populated
    result = await apply(
        factory,
        "dept.batch_delete" if batch else "dept.delete",
        [404, 405] if batch else None,
        None if batch else 405,
    )
    assert result["result"] == (2 if batch else 1)
    assert await rows(
        plain,
        select(Dept.dept_id).where(Dept.dept_id.in_([404, 405])).order_by(Dept.dept_id),
    ) == ([] if batch else [(404,)])
    assert await read_epoch(plain) == 2


async def test_permission_revoked_in_current_database_rejects_delete(populated):
    factory, plain = populated
    async with plain() as db:
        menu_id = await db.scalar(
            select(Menu.menu_id).where(Menu.permission == "system:user:delete")
        )
        await db.execute(
            delete(role_menus).where(
                role_menus.c.role_id == 201, role_menus.c.menu_id == menu_id
            )
        )
        await db.commit()
    with pytest.raises(BusinessException) as failure:
        await apply(factory, "user.delete", target_id=103)
    assert failure.value.error_code == "MISSING_PERMISSION"
    assert await read_epoch(plain) == 1


async def test_user_delete_permission_does_not_require_super_admin(populated):
    factory, plain = populated
    async with plain() as db:
        await db.execute(
            update(Role).where(Role.role_id == 201).values(role_code="R_MANAGER")
        )
        await db.commit()
    await apply(factory, "user.delete", target_id=103)
    assert await rows(plain, select(User.user_id).where(User.user_id == 103)) == []
    assert await read_epoch(plain) == 2


async def test_batch_delete_rejects_any_out_of_scope_target_before_deleting(populated):
    factory, plain = populated
    async with plain() as db:
        await db.execute(
            update(Role)
            .where(Role.role_id == 201)
            .values(role_code="R_MANAGER", data_scope="2")
        )
        await db.execute(
            insert(role_depts).values(tenant_id=0, role_id=201, dept_id=402)
        )
        await db.commit()
    with pytest.raises(BusinessException) as failure:
        await apply(factory, "user.batch_delete", [103, 104])
    assert failure.value.error_code == "USER_ROLE_TARGET_OUT_OF_SCOPE"
    assert await rows(
        plain,
        select(User.user_id).where(User.user_id.in_([103, 104])).order_by(User.user_id),
    ) == [(103,), (104,)]
    assert await rows(
        plain, select(user_depts.c.user_id).where(user_depts.c.user_id == 103)
    ) == [(103,)]
    assert await read_epoch(plain) == 1


async def test_profile_nullable_fields_are_self_only(populated):
    factory, plain = populated
    async with plain() as db:
        await db.execute(
            update(User)
            .where(User.user_id == 101)
            .values(user_email="actor@example.com", email_verified_at=datetime.now(UTC))
        )
        await db.commit()
    await apply(
        factory, "user.profile", UpdateProfile(user_email=None, nickname="myself"), 103
    )
    assert await rows(
        plain,
        select(User.nickname, User.user_email, User.email_verified_at).where(
            User.user_id == 101
        ),
    ) == [("myself", None, None)]
    assert await rows(plain, select(User.user_email).where(User.user_id == 103)) == [
        ("user103@example.com",)
    ]
    assert await read_epoch(plain) == 2


@pytest.mark.parametrize("dependency", ["author", "rating"])
async def test_user_delete_never_silently_changes_marketplace_history(
    populated, dependency
):
    factory, plain = populated
    async with plain() as db:
        await db.execute(
            insert(App).values(
                id=701,
                tenant_id=0,
                name="history",
                slug="history",
                type="backend",
                category="test",
                author_id=103 if dependency == "author" else None,
            )
        )
        if dependency == "rating":
            await db.execute(
                insert(AppRating).values(id=702, app_id=701, user_id=103, rating=4)
            )
        await db.commit()
    with pytest.raises(BusinessException) as failure:
        await apply(factory, "user.delete", target_id=103)
    assert failure.value.error_code == "FIELD_POLICY_LOCK_ORDER"
    assert await rows(plain, select(User.user_id).where(User.user_id == 103)) == [
        (103,)
    ]
    assert await rows(
        plain, select(user_roles.c.user_id).where(user_roles.c.user_id == 103)
    ) == [(103,)]
    if dependency == "author":
        assert await rows(plain, select(App.author_id).where(App.id == 701)) == [(103,)]
    else:
        assert await rows(
            plain, select(AppRating.user_id).where(AppRating.id == 702)
        ) == [(103,)]
    assert await read_epoch(plain) == 1
