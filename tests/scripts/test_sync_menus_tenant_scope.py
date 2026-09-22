"""Default-tenant menu synchronization must ignore other tenants' definitions."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.core.id_generator import next_id
from app.modules.system.models.menu import Menu
from scripts import sync_menus as module
from tests.tenant_helpers import create_test_tenant


async def test_sync_uses_default_tenant_routes_permissions_and_parents(
    db_session, monkeypatch
):
    other = await create_test_tenant(db_session, prefix="menu-scope")
    default_parent_id, other_parent_id = next_id(), next_id()
    for tenant_id, menu_id in (
        (0, default_parent_id),
        (other.tenant_id, other_parent_id),
    ):
        db_session.add(
            Menu(
                tenant_id=tenant_id,
                menu_id=menu_id,
                menu_name="Parent",
                menu_type="M",
                route_name="test_scope_parent",
                order=0,
                status="1",
            )
        )
    db_session.add(
        Menu(
            tenant_id=other.tenant_id,
            menu_id=next_id(),
            menu_name="Other child",
            menu_type="C",
            route_name="test_scope_child",
            parent_id=other_parent_id,
            permission="test:scope:use",
            order=0,
            status="1",
        )
    )
    await db_session.flush()
    monkeypatch.setattr(
        module,
        "MENU_DEFINITIONS",
        [
            {
                "route_name": "test_scope_child",
                "parent_route": "test_scope_parent",
                "menu_name": "Child",
                "menu_type": "C",
                "order": 0,
            },
            {
                "route_name": "test_scope_child",
                "parent_route": "test_scope_child",
                "menu_name": "Use",
                "menu_type": "F",
                "permission": "test:scope:use",
                "order": 0,
            },
        ],
    )
    for name in (
        "_retire_platform_only_tenant_menus",
        "_refactor_ai_first_home",
        "_reconcile_menu_partitions",
    ):
        monkeypatch.setattr(module, name, AsyncMock(return_value=0))
    monkeypatch.setattr(
        module,
        "create_async_engine",
        lambda *_args: SimpleNamespace(dispose=AsyncMock()),
    )

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr(module, "sessionmaker", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(db_session, "commit", AsyncMock(side_effect=db_session.flush))
    await module.sync_menus()
    child = await db_session.scalar(
        select(Menu).where(Menu.tenant_id == 0, Menu.route_name == "test_scope_child")
    )
    assert child is not None
    assert child.parent_id == default_parent_id
    button = await db_session.scalar(
        select(Menu).where(Menu.tenant_id == 0, Menu.permission == "test:scope:use")
    )
    assert button is not None
    assert button.parent_id == child.menu_id
