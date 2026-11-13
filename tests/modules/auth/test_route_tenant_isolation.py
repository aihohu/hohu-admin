"""Runtime navigation and management existence keep the current tenant scope."""

from sqlalchemy import insert, select, update

from app.core.tenant import TenantContext
from app.db.authorization_runtime import AuthorizationRuntime
from app.db.base import user_roles
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


def route_names(routes):
    return {
        name
        for route in routes
        for name in ({route["name"]} | route_names(route.get("children", [])))
    }


async def test_super_admin_routes_and_existence_are_tenant_scoped(system_sessions):
    _, plain = system_sessions
    async with plain() as db:
        await db.execute(
            update(Role)
            .where(Role.tenant_id == 1, Role.role_id == 202)
            .values(role_code="R_SUPER")
        )
        await db.execute(
            insert(user_roles).values(tenant_id=1, user_id=102, role_id=202)
        )
        for tenant, mid, name in (
            (0, 901, "default_only"),
            (1, 902, "tenant_only"),
            (1, 903, "ai_agent"),
        ):
            await db.execute(
                insert(Menu).values(
                    tenant_id=tenant,
                    menu_id=mid,
                    menu_name=name,
                    menu_type="C",
                    component="layout.base$view.home",
                    route_name=name,
                    route_path="/" + name,
                    status="1",
                )
            )
        await db.commit()
    runtime = AuthorizationRuntime(plain.kw["bind"])
    identity = {
        "tenant": TenantContext(1, "plan-1", 102, 1, "access_token"),
        "actor_user_version": 1,
    }
    response = await runtime.relation_read(**identity, operation="runtime.routes")
    names = route_names(response["routes"])
    assert (
        "tenant_only" in names
        and "default_only" not in names
        and "ai_agent" not in names
    )
    for name, expected in (
        ("tenant_only", True),
        ("default_only", False),
        ("ai_agent", False),
    ):
        assert (
            await runtime.relation_read(
                **identity, operation="runtime.exists", query=name
            )
            is expected
        )
    async with plain() as db:
        persisted = await db.scalar(
            select(Menu).where(Menu.tenant_id == 1, Menu.route_name == "tenant_only")
        )
        assert persisted.menu_id == 902 and persisted.tenant_id == 1
