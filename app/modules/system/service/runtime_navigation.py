"""Minimal runtime navigation is a fixed authenticated identity protocol."""

from types import SimpleNamespace

from sqlalchemy import select

from app.core.field_policy.contracts import limit, require
from app.core.rbac import is_super_admin, is_system_admin
from app.modules.auth.schemas.auth import RouteMeta, UserRoute
from app.modules.auth.service import build_menu_tree
from app.modules.system.constants import PLATFORM_ONLY_TENANT_ROUTE_NAMES
from app.modules.system.models.menu import Menu
from app.modules.system.service.user_field_context import load_current_user


async def runtime_routes(db, tx, *, tenant, actor_user_version):
    tx.assert_session(db)
    tx.assert_barrier(tenant.tenant_id)
    current_user = await load_current_user(
        db, tenant=tenant, actor_user_version=actor_user_version
    )
    names = (
        "menu_id",
        "parent_id",
        "menu_type",
        "route_name",
        "route_path",
        "component",
        "menu_name",
        "i18n_key",
        "keep_alive",
        "constant",
        "icon",
        "order",
        "href",
        "hide_in_menu",
        "active_menu",
        "multi_tab",
    )
    table = Menu.__table__
    statement = select(*(table.c[n] for n in names)).where(
        table.c.tenant_id == tenant.tenant_id,
        table.c.menu_type.in_(("M", "C")),
        table.c.status == "1",
    )
    if not is_super_admin(current_user):
        allowed = {
            m.menu_id
            for role in current_user.roles
            if role.status == "1"
            for m in role.menus
        }
        statement = statement.where(table.c.menu_id.in_(allowed))
    rows = (
        (
            await db.execute(
                statement.order_by(table.c.order, table.c.menu_id).limit(8193)
            )
        )
        .mappings()
        .all()
    )
    limit(len(rows) <= 8192)
    menu_list = [
        SimpleNamespace(**row)
        for row in rows
        if row["route_name"] not in PLATFORM_ONLY_TENANT_ROUTE_NAMES
    ]
    by_id = {m.menu_id: m for m in menu_list}
    for menu in menu_list:
        seen = {menu.menu_id}
        parent = menu.parent_id
        while parent in by_id:
            require(parent not in seen)
            seen.add(parent)
            limit(len(seen) <= 16)
            parent = by_id[parent].parent_id
    route_tree = build_menu_tree(menu_list, 0)
    if is_system_admin(current_user):
        route_tree.append(
            UserRoute(
                name="tenant",
                path="/tenant",
                component="layout.base$view.tenant",
                meta=RouteMeta(
                    title="租户管理",
                    i18n_key="route.tenant",
                    icon="ph:buildings",
                    order=3,
                ),
            )
        )
        agent_route = UserRoute(
            name="ai_agent",
            path="/ai/agent",
            component="view.ai_agent",
            meta=RouteMeta(
                title="Agent 管理", i18n_key="route.ai_agent", icon="ph:robot", order=2
            ),
        )
        provider_route = UserRoute(
            name="ai_provider",
            path="/ai/provider",
            component="view.ai_provider",
            meta=RouteMeta(
                title="模型管理",
                i18n_key="route.ai_provider",
                icon="carbon:settings-adjust",
                order=1,
            ),
        )
        ai_route = next((route for route in route_tree if route.name == "ai"), None)
        if ai_route is None:
            ai_route = UserRoute(
                name="ai",
                path="/ai",
                component="layout.base",
                meta=RouteMeta(title="AI 管理", i18n_key="route.ai", icon="ph:robot"),
                children=[],
            )
            route_tree.append(ai_route)
        ai_route.children = [*(ai_route.children or []), provider_route, agent_route]

    return {
        "home": "ai_chat",
        "routes": [
            route.model_dump(mode="json", by_alias=True, exclude_none=True)
            for route in route_tree
        ],
    }
