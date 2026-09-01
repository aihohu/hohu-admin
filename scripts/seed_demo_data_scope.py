"""Seed demo data for the data-scope-demo page.

演示数据权限：建一组角色（5 种 scope 各一）+ 用户（每个挂一个角色）+
部门（3 层）+ 30 条业务数据，演示者用不同账号登录 admin-web 的「数据
权限演示」页面，看到同一份数据的不同子集。

幂等：所有 ID 固定常量；用户按固定 ID 对账并迁移旧用户名，缺失用户及关联
单独补齐；其余实体按稳定业务键检查。重跑安全，也可修复部分执行后的数据。

Usage:
    cd hohu-admin
    python scripts/seed_demo_data_scope.py

演示账号（密码统一 demo@12345）：
    demoall      ALL          看全部 30 条
    demodeptsub  DEPT_AND_SUB 看主部门 BRANCH_A 及子（约 20 条）
    demodept     DEPT         仅看主部门 BRANCH_A（约 10 条）
    democustom   CUSTOM       看 role_depts 配置的 TEAM_A1+TEAM_B1（约 10 条）
    demoself     SELF         仅看自己创建的（约 5 条）
"""

# ruff: noqa: T201

import asyncio

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.constants import (
    DATA_SCOPE_ALL,
    DATA_SCOPE_CUSTOM,
    DATA_SCOPE_DEPT,
    DATA_SCOPE_DEPT_AND_SUB,
    DATA_SCOPE_SELF,
    STATUS_ENABLED,
)
from app.core.config import settings
from app.core.id_generator import next_id
from app.core.security import get_password_hash
from app.core.tenant import DEFAULT_TENANT_ID
from app.db.base import role_depts, role_menus, user_depts, user_roles
from app.modules.system.models.data_scope_demo import DataScopeDemo
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User

# 固定 ID（避免与测试 fixture 的 1001-1006/5001-5005/900000xxx 冲突）
ROOT_ID = 800000001
BRANCH_A_ID = 800000002
TEAM_A1_ID = 800000003
TEAM_A2_ID = 800000004
BRANCH_B_ID = 800000005
TEAM_B1_ID = 800000006

ROLE_ALL_ID = 800000101
ROLE_DEPT_SUB_ID = 800000102
ROLE_DEPT_ID = 800000103
ROLE_CUSTOM_ID = 800000104
ROLE_SELF_ID = 800000105

USER_ALL_ID = 800000201
USER_DEPT_SUB_ID = 800000202
USER_DEPT_ID = 800000203
USER_CUSTOM_ID = 800000204
USER_SELF_ID = 800000205

PASSWORD = "demo@12345"

DEPTS = [
    (ROOT_ID, "总公司", "0"),
    (BRANCH_A_ID, "华东分公司", f"0,{ROOT_ID}"),
    (TEAM_A1_ID, "华东-销售组", f"0,{ROOT_ID},{BRANCH_A_ID}"),
    (TEAM_A2_ID, "华东-客服组", f"0,{ROOT_ID},{BRANCH_A_ID}"),
    (BRANCH_B_ID, "华南分公司", f"0,{ROOT_ID}"),
    (TEAM_B1_ID, "华南-销售组", f"0,{ROOT_ID},{BRANCH_B_ID}"),
]

ROLES = [
    (ROLE_ALL_ID, "演示-全部数据", "R_DEMO_ALL", DATA_SCOPE_ALL),
    (ROLE_DEPT_SUB_ID, "演示-本部门及以下", "R_DEMO_DEPT_SUB", DATA_SCOPE_DEPT_AND_SUB),
    (ROLE_DEPT_ID, "演示-本部门", "R_DEMO_DEPT", DATA_SCOPE_DEPT),
    (ROLE_CUSTOM_ID, "演示-自定义", "R_DEMO_CUSTOM", DATA_SCOPE_CUSTOM),
    (ROLE_SELF_ID, "演示-仅本人", "R_DEMO_SELF", DATA_SCOPE_SELF),
]

# 每个用户的 (user_id, user_name, nickname, role_id, primary_dept_id, [dept_ids])
USERS = [
    (USER_ALL_ID, "demoall", "演示-全部", ROLE_ALL_ID, TEAM_A1_ID, [TEAM_A1_ID]),
    (
        USER_DEPT_SUB_ID,
        "demodeptsub",
        "演示-本部门及以下",
        ROLE_DEPT_SUB_ID,
        BRANCH_A_ID,
        [BRANCH_A_ID],
    ),
    (
        USER_DEPT_ID,
        "demodept",
        "演示-本部门",
        ROLE_DEPT_ID,
        BRANCH_A_ID,
        [BRANCH_A_ID],
    ),
    (
        USER_CUSTOM_ID,
        "democustom",
        "演示-自定义",
        ROLE_CUSTOM_ID,
        BRANCH_B_ID,
        [BRANCH_B_ID],
    ),
    (USER_SELF_ID, "demoself", "演示-仅本人", ROLE_SELF_ID, TEAM_A1_ID, [TEAM_A1_ID]),
]

LEGACY_USER_NAMES = {
    USER_ALL_ID: "demo_all",
    USER_DEPT_SUB_ID: "demo_dept_sub",
    USER_DEPT_ID: "demo_dept",
    USER_CUSTOM_ID: "demo_custom",
    USER_SELF_ID: "demo_self",
}


async def _seed_depts(db: AsyncSession) -> None:
    existing = (
        (
            await db.execute(
                select(Dept.dept_id).where(
                    Dept.tenant_id == DEFAULT_TENANT_ID,
                    Dept.dept_id.in_([d[0] for d in DEPTS]),
                )
            )
        )
        .scalars()
        .all()
    )
    if existing:
        print(f"  depts: {len(existing)} already exist, skip")
        return
    db.add_all(
        [
            Dept(
                tenant_id=DEFAULT_TENANT_ID,
                dept_id=did,
                dept_name=name,
                ancestors=anc,
                order_num=i,
                status=STATUS_ENABLED,
            )
            for i, (did, name, anc) in enumerate(DEPTS)
        ]
    )
    await db.flush()
    print(f"  depts: created {len(DEPTS)}")


async def _seed_roles(db: AsyncSession) -> None:
    existing_codes = (
        (
            await db.execute(
                select(Role.role_code).where(
                    Role.tenant_id == DEFAULT_TENANT_ID,
                    Role.role_code.in_([r[2] for r in ROLES]),
                )
            )
        )
        .scalars()
        .all()
    )
    if existing_codes:
        print(f"  roles: {len(existing_codes)} already exist, skip")
        return
    db.add_all(
        [
            Role(
                tenant_id=DEFAULT_TENANT_ID,
                role_id=rid,
                role_name=name,
                role_code=code,
                data_scope=scope,
                status=STATUS_ENABLED,
            )
            for rid, name, code, scope in ROLES
        ]
    )
    await db.flush()
    print(f"  roles: created {len(ROLES)}")

    # CUSTOM 角色配 role_depts：TEAM_A1 + TEAM_B1（跨分公司，演示效果明显）
    await db.execute(
        insert(role_depts).values(
            [
                {
                    "tenant_id": DEFAULT_TENANT_ID,
                    "role_id": ROLE_CUSTOM_ID,
                    "dept_id": TEAM_A1_ID,
                },
                {
                    "tenant_id": DEFAULT_TENANT_ID,
                    "role_id": ROLE_CUSTOM_ID,
                    "dept_id": TEAM_B1_ID,
                },
            ]
        )
    )
    print("  role_depts: R_DEMO_CUSTOM -> [TEAM_A1, TEAM_B1]")


async def _seed_users(db: AsyncSession) -> None:
    desired_names = {user_id: user_name for user_id, user_name, *_rest in USERS}
    for user_id, legacy_name in LEGACY_USER_NAMES.items():
        await db.execute(
            update(User)
            .where(
                User.tenant_id == DEFAULT_TENANT_ID,
                User.user_id == user_id,
                User.user_name == legacy_name,
            )
            .values(user_name=desired_names[user_id])
        )

    existing_ids = set(
        (
            await db.execute(
                select(User.user_id).where(
                    User.tenant_id == DEFAULT_TENANT_ID,
                    User.user_id.in_([u[0] for u in USERS]),
                )
            )
        )
        .scalars()
        .all()
    )
    missing_users = [user for user in USERS if user[0] not in existing_ids]
    hashed = get_password_hash(PASSWORD) if missing_users else ""
    for (
        user_id,
        user_name,
        nickname,
        _role_id,
        _primary_dept_id,
        _dept_ids,
    ) in missing_users:
        db.add(
            User(
                tenant_id=DEFAULT_TENANT_ID,
                user_id=user_id,
                user_name=user_name,
                nickname=nickname,
                hashed_password=hashed,
                status=STATUS_ENABLED,
            )
        )
    await db.flush()

    user_role_rows = [
        {"tenant_id": DEFAULT_TENANT_ID, "user_id": u[0], "role_id": u[3]}
        for u in USERS
    ]
    user_dept_rows = [
        {
            "tenant_id": DEFAULT_TENANT_ID,
            "user_id": u[0],
            "dept_id": did,
            "is_primary": "Y" if did == u[4] else "N",
        }
        for u in USERS
        for did in u[5]
    ]
    await db.execute(
        pg_insert(user_roles)
        .values(user_role_rows)
        .on_conflict_do_nothing(index_elements=["tenant_id", "user_id", "role_id"])
    )
    await db.execute(
        pg_insert(user_depts)
        .values(user_dept_rows)
        .on_conflict_do_nothing(index_elements=["tenant_id", "user_id", "dept_id"])
    )
    print(
        f"  users: created {len(missing_users)}, reconciled {len(existing_ids)} "
        f"(password for new users: {PASSWORD})"
    )


async def _seed_demo_data(db: AsyncSession) -> None:
    """30 条数据：均匀分布到 6 个部门 + 5 个用户，让各种 scope 看到差异。"""
    existing = (
        (
            await db.execute(
                select(DataScopeDemo.demo_id).where(
                    DataScopeDemo.tenant_id == DEFAULT_TENANT_ID,
                    DataScopeDemo.title.like("演示数据-%"),
                )
            )
        )
        .scalars()
        .all()
    )
    if existing:
        print(f"  demo data: {len(existing)} already exist, skip")
        return

    dept_ids = [TEAM_A1_ID, TEAM_A2_ID, BRANCH_A_ID, BRANCH_B_ID, TEAM_B1_ID, ROOT_ID]
    creator_ids = [
        USER_ALL_ID,
        USER_DEPT_SUB_ID,
        USER_DEPT_ID,
        USER_CUSTOM_ID,
        USER_SELF_ID,
    ]

    rows = []
    for i in range(30):
        rows.append(
            DataScopeDemo(
                tenant_id=DEFAULT_TENANT_ID,
                demo_id=next_id(),
                title=f"演示数据-{i + 1:02d}",
                content=f"这是第 {i + 1} 条数据，用于演示数据权限。",
                dept_id=dept_ids[i % len(dept_ids)],
                create_by=creator_ids[i % len(creator_ids)],
                status=STATUS_ENABLED,
            )
        )
    db.add_all(rows)
    print(f"  demo data: created {len(rows)}")


async def _assign_role_menus(db: AsyncSession) -> None:
    """给 5 个演示角色分配菜单权限：首页 + 数据权限演示（含 4 个按钮）。

    没有这些菜单权限，演示账号登录后侧边栏空白、无法访问演示页。
    按钮权限一并分配，否则页面打开但 list/add/edit/delete API 被 403 拒绝。
    """
    # 1. 找出需要的 menu_id：
    #    - home（首页）
    #    - system_data-scope-demo（演示页菜单本身）
    #    - parent_id == system_data-scope-demo 的所有 F 类型按钮（list/add/edit/delete）
    target_routes = ["home", "system_data-scope-demo"]
    demo_menu = (
        (
            await db.execute(
                select(Menu.menu_id).where(
                    Menu.tenant_id == DEFAULT_TENANT_ID,
                    Menu.route_name == "system_data-scope-demo",
                )
            )
        )
        .scalars()
        .first()
    )
    if not demo_menu:
        print(
            "  role_menus: SKIP - system_data-scope-demo menu not found (run sync_menus.py first)"
        )
        return

    menu_rows = (
        (
            await db.execute(
                select(Menu.menu_id).where(
                    Menu.tenant_id == DEFAULT_TENANT_ID,
                    (Menu.route_name.in_(target_routes))
                    | ((Menu.parent_id == demo_menu) & (Menu.menu_type == "F")),
                )
            )
        )
        .scalars()
        .all()
    )
    if not menu_rows:
        print("  role_menus: SKIP - target menus not found")
        return

    # 2. 5 个演示角色的 role_id
    role_ids = [r[0] for r in ROLES]

    # 3. ON CONFLICT DO NOTHING 批量插入 role_menus（复合主键 role_id+menu_id）
    rows = [
        {"tenant_id": DEFAULT_TENANT_ID, "role_id": rid, "menu_id": mid}
        for rid in role_ids
        for mid in menu_rows
    ]
    stmt = (
        pg_insert(role_menus)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["tenant_id", "role_id", "menu_id"])
    )
    result = await db.execute(stmt)
    print(
        f"  role_menus: {len(menu_rows)} menus/buttons × {len(role_ids)} roles "
        f"= {len(rows)} planned, {result.rowcount} new"
    )


async def seed() -> None:
    print("Seeding data scope demo...")
    engine = create_async_engine(settings.DATABASE_URL)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as db:
        await _seed_depts(db)
        await _seed_roles(db)
        await _seed_users(db)
        await _seed_demo_data(db)
        await _assign_role_menus(db)
        await db.commit()

    await engine.dispose()
    print("\nDone. 演示账号（密码统一 demo@12345）：")
    for user_name, nickname in [(u[1], u[2]) for u in USERS]:
        print(f"  {user_name:<16} {nickname}")


if __name__ == "__main__":
    asyncio.run(seed())
