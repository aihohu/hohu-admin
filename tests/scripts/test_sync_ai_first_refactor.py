"""AI-first home refactor: MENU_DEFINITIONS shape and the one-shot migration."""

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.id_generator import next_id
from app.core.tenant import DEFAULT_TENANT_ID
from app.db.base import role_menus
from app.modules.system.models.menu import Menu
from scripts.sync_menus import MENU_DEFINITIONS, _refactor_ai_first_home


def _def_by_route(name: str) -> dict:
    return next(d for d in MENU_DEFINITIONS if d.get("route_name") == name)


def test_definitions_drop_home_and_promote_ai_chat() -> None:
    routes = {d.get("route_name") for d in MENU_DEFINITIONS}
    assert "home" not in routes

    ai_chat = _def_by_route("ai_chat")
    assert ai_chat["parent_route"] == "0"
    assert ai_chat["menu_name"] == "AI 助手"
    assert ai_chat["component"] == "layout.base$view.ai_chat"
    assert ai_chat["layout"] == "base"
    assert ai_chat["order"] == 0

    ai_group = _def_by_route("ai")
    assert ai_group["menu_name"] == "AI 管理"

    dashboard = _def_by_route("dashboard")
    assert dashboard["parent_route"] == "system"
    assert dashboard["component"] == "view.dashboard"


async def _add_old_shape(db: AsyncSession) -> Menu:
    """Restore the legacy menu shape inside the rolled-back transaction.

    The ai/system groups keep their real children (FK RESTRICT), so only
    missing rows are re-created and ai_chat is demoted back to a child.
    """

    async def _get(route_name: str) -> Menu | None:
        return await db.scalar(
            select(Menu).where(
                Menu.tenant_id == DEFAULT_TENANT_ID, Menu.route_name == route_name
            )
        )

    dashboard = await _get("dashboard")
    if dashboard is not None:
        await db.execute(
            delete(role_menus).where(role_menus.c.menu_id == dashboard.menu_id)
        )
        await db.delete(dashboard)

    home = await _get("home")
    if home is None:
        home = Menu(
            tenant_id=DEFAULT_TENANT_ID,
            menu_id=next_id(),
            parent_id=0,
            menu_name="首页",
            menu_type="C",
            route_name="home",
            route_path="/home",
            component="layout.base$view.home",
            layout="base",
            page="home",
            status="1",
        )
        db.add(home)

    ai_group = await _get("ai")
    if ai_group is None:
        ai_group = Menu(
            tenant_id=DEFAULT_TENANT_ID,
            menu_id=next_id(),
            parent_id=0,
            menu_name="AI 助手",
            menu_type="M",
            route_name="ai",
            route_path="/ai",
            component="layout.base",
            status="1",
        )
        db.add(ai_group)
    else:
        ai_group.menu_name = "AI 助手"

    ai_chat = await _get("ai_chat")
    if ai_chat is None:
        ai_chat = Menu(
            tenant_id=DEFAULT_TENANT_ID,
            menu_id=next_id(),
            menu_name="AI 对话",
            menu_type="C",
            route_name="ai_chat",
            route_path="/ai/chat",
            component="view.ai_chat",
            status="1",
        )
        db.add(ai_chat)
    await db.flush()
    ai_chat.parent_id = ai_group.menu_id
    ai_chat.menu_name = "AI 对话"
    ai_chat.component = "view.ai_chat"
    ai_chat.layout = None
    ai_chat.order = 1

    system = await _get("system")
    assert system is not None
    await db.flush()
    return system


async def test_refactor_migrates_old_structure(db_session: AsyncSession) -> None:
    system = await _add_old_shape(db_session)
    assert system is not None

    note = await _refactor_ai_first_home(db_session)
    assert note is not None

    home = await db_session.scalar(
        select(Menu).where(
            Menu.tenant_id == DEFAULT_TENANT_ID, Menu.route_name == "home"
        )
    )
    assert home is None

    ai_chat = await db_session.scalar(
        select(Menu).where(
            Menu.tenant_id == DEFAULT_TENANT_ID, Menu.route_name == "ai_chat"
        )
    )
    assert ai_chat.parent_id is None  # @validates maps a 0 parent to NULL
    assert ai_chat.menu_name == "AI 助手"
    assert ai_chat.component == "layout.base$view.ai_chat"
    assert ai_chat.layout == "base"

    ai_group = await db_session.scalar(
        select(Menu).where(Menu.tenant_id == DEFAULT_TENANT_ID, Menu.route_name == "ai")
    )
    assert ai_group.menu_name == "AI 管理"

    dashboard = await db_session.scalar(
        select(Menu).where(
            Menu.tenant_id == DEFAULT_TENANT_ID, Menu.route_name == "dashboard"
        )
    )
    assert dashboard is not None
    assert dashboard.parent_id == system.menu_id
    assert dashboard.component == "view.dashboard"


async def test_refactor_is_idempotent_when_home_absent(
    db_session: AsyncSession,
) -> None:
    await _add_old_shape(db_session)
    first = await _refactor_ai_first_home(db_session)
    assert first is not None

    second = await _refactor_ai_first_home(db_session)
    assert second is None
