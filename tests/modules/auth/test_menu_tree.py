from app.core.id_generator import next_id
from app.modules.auth.service import build_menu_tree
from app.modules.system.models.menu import Menu


def _menu(
    *,
    route_name: str,
    parent_id: int,
    menu_type: str,
    component: str,
) -> Menu:
    return Menu(
        menu_id=next_id(),
        parent_id=parent_id,
        menu_name=route_name,
        menu_type=menu_type,
        component=component,
        route_name=route_name,
        route_path=f"/{route_name}",
        status="1",
    )


def test_layout_directory_without_visible_children_is_not_emitted() -> None:
    orphan = _menu(
        route_name="auth",
        parent_id=0,
        menu_type="M",
        component="layout.base",
    )

    assert build_menu_tree([orphan], 0) == []


def test_layout_directory_with_a_page_child_is_emitted() -> None:
    parent = _menu(
        route_name="system",
        parent_id=0,
        menu_type="M",
        component="layout.base",
    )
    child = _menu(
        route_name="system_user",
        parent_id=parent.menu_id,
        menu_type="C",
        component="view.system_user",
    )

    routes = build_menu_tree([parent, child], 0)

    assert [route.name for route in routes] == ["system"]
    assert routes[0].children is not None
    assert [route.name for route in routes[0].children] == ["system_user"]


def test_single_level_layout_and_view_component_is_not_pruned() -> None:
    route = _menu(
        route_name="home",
        parent_id=0,
        menu_type="M",
        component="layout.base$view.home",
    )

    assert [item.name for item in build_menu_tree([route], 0)] == ["home"]
