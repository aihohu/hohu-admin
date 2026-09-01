"""Phase 2 multi-role data-scope union regression tests."""

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.constants import (
    DATA_SCOPE_ALL,
    DATA_SCOPE_CUSTOM,
    DATA_SCOPE_DEPT,
    DATA_SCOPE_DEPT_AND_SUB,
    STATUS_DISABLED,
    STATUS_ENABLED,
)
from app.core.id_generator import next_id
from app.db.base import role_depts, user_depts, user_roles
from app.modules.ai.core.data_scope_loader import build_data_scope_context
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.utils.data_scope import (
    get_user_data_scope_filters,
    resolve_data_scope,
)
from tests.tenant_helpers import bind_test_user, tenant_context

TENANT = tenant_context()


async def _principal(
    db: AsyncSession,
    *,
    scopes: list[tuple[str, list[int]]],
    dept_ids: list[int],
    disabled_scopes: list[tuple[str, list[int]]] | None = None,
) -> User:
    user_id = next_id()
    roles: list[Role] = []
    for index, (scope, custom_dept_ids) in enumerate(
        [*scopes, *(disabled_scopes or [])]
    ):
        enabled = index < len(scopes)
        role = Role(
            tenant_id=TENANT.tenant_id,
            role_id=next_id(),
            role_name=f"scope-{user_id}-{index}",
            role_code=f"R_SCOPE_{user_id}_{index}",
            data_scope=scope,
            status=STATUS_ENABLED if enabled else STATUS_DISABLED,
        )
        db.add(role)
        roles.append(role)
        await db.flush()
        if custom_dept_ids:
            await db.execute(
                insert(role_depts),
                [
                    {
                        "tenant_id": TENANT.tenant_id,
                        "role_id": role.role_id,
                        "dept_id": dept_id,
                    }
                    for dept_id in custom_dept_ids
                ],
            )

    actor = User(
        tenant_id=TENANT.tenant_id,
        user_id=user_id,
        user_name=f"scope-actor-{user_id}",
        nickname="scope actor",
        hashed_password="x",
        status=STATUS_ENABLED,
    )
    db.add(actor)
    await db.flush()
    await db.execute(
        insert(user_roles),
        [
            {
                "tenant_id": TENANT.tenant_id,
                "user_id": user_id,
                "role_id": role.role_id,
            }
            for role in roles
        ],
    )
    if dept_ids:
        await db.execute(
            insert(user_depts),
            [
                {
                    "tenant_id": TENANT.tenant_id,
                    "user_id": user_id,
                    "dept_id": dept_id,
                    "is_primary": "N",
                }
                for dept_id in dept_ids
            ],
        )
    await db.flush()
    result = await db.execute(
        select(User)
        .where(
            User.tenant_id == TENANT.tenant_id,
            User.user_id == user_id,
        )
        .options(
            selectinload(User.roles).selectinload(Role.depts),
            selectinload(User.roles).selectinload(Role.menus),
            selectinload(User.depts),
        )
    )
    principal = result.scalar_one()
    bind_test_user(principal)
    return principal


async def _subject(db: AsyncSession, *, dept_id: int, label: str) -> int:
    user_id = next_id()
    db.add(
        User(
            tenant_id=TENANT.tenant_id,
            user_id=user_id,
            user_name=f"scope-{label}-{user_id}",
            nickname=label,
            hashed_password="x",
            status=STATUS_ENABLED,
        )
    )
    await db.flush()
    await db.execute(
        insert(user_depts).values(
            tenant_id=TENANT.tenant_id,
            user_id=user_id,
            dept_id=dept_id,
            is_primary="N",
        )
    )
    return user_id


async def _visible_user_ids(
    db: AsyncSession,
    *,
    filters: list,
) -> set[int]:
    result = await db.execute(select(User.user_id).where(*filters))
    return set(result.scalars())


async def test_dept_and_custom_roles_are_materialized_as_a_union(
    db_session: AsyncSession,
) -> None:
    own = Dept(
        tenant_id=TENANT.tenant_id,
        dept_id=next_id(),
        dept_name=f"own-{next_id()}",
        ancestors="0",
        order_num=0,
        status=STATUS_ENABLED,
    )
    custom = Dept(
        tenant_id=TENANT.tenant_id,
        dept_id=next_id(),
        dept_name=f"custom-{next_id()}",
        ancestors="0",
        order_num=0,
        status=STATUS_ENABLED,
    )
    disabled_only = Dept(
        tenant_id=TENANT.tenant_id,
        dept_id=next_id(),
        dept_name=f"disabled-{next_id()}",
        ancestors="0",
        order_num=0,
        status=STATUS_ENABLED,
    )
    db_session.add_all([own, custom, disabled_only])
    await db_session.flush()
    actor = await _principal(
        db_session,
        scopes=[(DATA_SCOPE_DEPT, []), (DATA_SCOPE_CUSTOM, [custom.dept_id])],
        disabled_scopes=[(DATA_SCOPE_CUSTOM, [disabled_only.dept_id])],
        dept_ids=[own.dept_id],
    )
    own_user = await _subject(db_session, dept_id=own.dept_id, label="own")
    custom_user = await _subject(db_session, dept_id=custom.dept_id, label="custom")
    disabled_user = await _subject(
        db_session,
        dept_id=disabled_only.dept_id,
        label="disabled",
    )
    await db_session.flush()

    resolution = await resolve_data_scope(db_session, actor, tenant=TENANT)
    filters = await get_user_data_scope_filters(db_session, actor, tenant=TENANT)
    ai_context = await build_data_scope_context(db_session, actor)

    assert resolution.scope_kinds == frozenset({DATA_SCOPE_DEPT, DATA_SCOPE_CUSTOM})
    assert resolution.accessible_dept_ids == frozenset({own.dept_id, custom.dept_id})
    assert await _visible_user_ids(db_session, filters=filters) >= {
        actor.user_id,
        own_user,
        custom_user,
    }
    assert disabled_user not in await _visible_user_ids(db_session, filters=filters)
    assert ai_context.accessible_dept_ids == {own.dept_id, custom.dept_id}
    ai_ids = set((await db_session.execute(ai_context.accessible_user_scope)).scalars())
    assert {actor.user_id, own_user, custom_user} <= ai_ids
    assert disabled_user not in ai_ids


async def test_dept_and_sub_and_custom_union_keeps_both_incomparable_sets(
    db_session: AsyncSession,
) -> None:
    root = Dept(
        tenant_id=TENANT.tenant_id,
        dept_id=next_id(),
        dept_name=f"root-{next_id()}",
        ancestors="0",
        order_num=0,
        status=STATUS_ENABLED,
    )
    child = Dept(
        tenant_id=TENANT.tenant_id,
        dept_id=next_id(),
        dept_name=f"child-{next_id()}",
        ancestors=f"0,{root.dept_id}",
        order_num=0,
        status=STATUS_ENABLED,
    )
    custom = Dept(
        tenant_id=TENANT.tenant_id,
        dept_id=next_id(),
        dept_name=f"custom-{next_id()}",
        ancestors="0",
        order_num=0,
        status=STATUS_ENABLED,
    )
    db_session.add_all([root, child, custom])
    await db_session.flush()
    actor = await _principal(
        db_session,
        scopes=[
            (DATA_SCOPE_DEPT_AND_SUB, []),
            (DATA_SCOPE_CUSTOM, [custom.dept_id]),
        ],
        dept_ids=[root.dept_id],
    )
    child_user = await _subject(db_session, dept_id=child.dept_id, label="child")
    custom_user = await _subject(db_session, dept_id=custom.dept_id, label="custom")
    await db_session.flush()

    resolution = await resolve_data_scope(db_session, actor, tenant=TENANT)
    filters = await get_user_data_scope_filters(db_session, actor, tenant=TENANT)

    assert resolution.accessible_dept_ids == frozenset(
        {root.dept_id, child.dept_id, custom.dept_id}
    )
    assert {child_user, custom_user} <= await _visible_user_ids(
        db_session,
        filters=filters,
    )


async def test_enabled_all_scope_is_explicitly_unbounded(
    db_session: AsyncSession,
) -> None:
    actor = await _principal(
        db_session,
        scopes=[(DATA_SCOPE_ALL, [])],
        dept_ids=[],
    )

    resolution = await resolve_data_scope(db_session, actor, tenant=TENANT)
    ai_context = await build_data_scope_context(db_session, actor)

    assert resolution.unbounded is True
    assert resolution.accessible_dept_ids is None
    assert resolution.accessible_user_scope is None
    assert ai_context.accessible_dept_ids is None
    assert ai_context.accessible_user_scope is None
    assert len(ai_context.filters) == 1
