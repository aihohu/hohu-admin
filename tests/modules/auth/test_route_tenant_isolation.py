from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import SUPER_ADMIN_ROLE_CODE
from app.core.id_generator import next_id
from app.db.session import AsyncSessionLocal, engine
from app.modules.auth.api import get_user_routes, is_route_exist
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.tenant import Tenant


@pytest.fixture
async def db_session() -> AsyncSession:
    async with engine.connect() as connection:
        outer = await connection.begin()
        try:
            async with AsyncSessionLocal(bind=connection) as session:
                yield session
        finally:
            await outer.rollback()
    try:
        await engine.dispose()
    except RuntimeError as exc:
        if "Event loop is closed" not in str(exc):
            raise


def _route_names(routes: list) -> set[str]:
    names: set[str] = set()
    for route in routes:
        names.add(route.name)
        names.update(_route_names(route.children or []))
    return names


async def test_super_admin_routes_and_existence_are_tenant_scoped(db_session):
    tenant_id = next_id()
    tenant = Tenant(
        tenant_id=tenant_id,
        tenant_code=f"routes-{tenant_id}",
        tenant_name="Routes Tenant",
        status="2",
        lifecycle_state="prepared",
        bootstrap_version=0,
    )
    default_only = Menu(
        tenant_id=0,
        parent_id=None,
        menu_name="Default only",
        menu_type="C",
        component="layout.base$view.home",
        route_name=f"default_only_{tenant_id}",
        route_path=f"/default-only-{tenant_id}",
        status="1",
        menu_id=next_id(),
    )
    tenant_only = Menu(
        tenant_id=tenant_id,
        parent_id=None,
        menu_name="Tenant only",
        menu_type="C",
        component="layout.base$view.home",
        route_name=f"tenant_only_{tenant_id}",
        route_path=f"/tenant-only-{tenant_id}",
        status="1",
        menu_id=next_id(),
    )
    db_session.add(tenant)
    await db_session.flush()
    db_session.add_all([default_only, tenant_only])
    await db_session.flush()
    current_user = SimpleNamespace(
        tenant_id=tenant_id,
        user_name="tenantadmin",
        roles=[Role(role_code=SUPER_ADMIN_ROLE_CODE, status="1")],
    )

    response = await get_user_routes(current_user=current_user, db=db_session)
    names = _route_names(response.data["routes"])
    assert tenant_only.route_name in names
    assert default_only.route_name not in names
    assert (
        await is_route_exist(
            route_name=tenant_only.route_name,
            current_user=current_user,
            db=db_session,
        )
    ).data is True
    assert (
        await is_route_exist(
            route_name=default_only.route_name,
            current_user=current_user,
            db=db_session,
        )
    ).data is False

    persisted = await db_session.scalar(
        select(Menu).where(
            Menu.tenant_id == tenant_id,
            Menu.route_name == tenant_only.route_name,
        )
    )
    assert persisted is tenant_only
