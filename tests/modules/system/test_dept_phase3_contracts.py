"""Phase 3 department permission, schema, and page API contracts."""

import inspect
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError

from app.modules.system import constants as system_constants
from app.modules.system.api.dept import router
from app.modules.system.schemas import dept as dept_schemas
from scripts.init_db import bind_fresh_role_permissions
from scripts.sync_menus import MENU_DEFINITIONS


def _route(path: str, method: str) -> APIRoute:
    matches = [
        route
        for route in router.routes
        if isinstance(route, APIRoute)
        and route.path == path
        and method in route.methods
    ]
    assert len(matches) == 1, (path, method, matches)
    return matches[0]


def _permission_codes(route: APIRoute) -> set[str]:
    codes: set[str] = set()

    def collect(dependant) -> None:  # noqa: ANN001
        for dependency in dependant.dependencies:
            if inspect.isfunction(dependency.call) or inspect.ismethod(dependency.call):
                nonlocals = inspect.getclosurevars(dependency.call).nonlocals
                permission = nonlocals.get("perm_code")
                if isinstance(permission, str):
                    codes.add(permission)
            collect(dependency)

    collect(route.dependant)
    return codes


def test_department_move_permission_is_a_distinct_seeded_capability() -> None:
    permission = getattr(system_constants, "DEPT_MOVE_PERMISSION", None)

    assert permission == "system:dept:move"
    assert permission in {
        item.get("permission")
        for item in MENU_DEFINITIONS
        if item.get("menu_type") == "F"
    }


def test_fresh_super_role_receives_explicit_department_tool_permissions() -> None:
    permissions = {
        "system:dept:list",
        "system:dept:add",
        "system:dept:edit",
        "system:dept:move",
    }
    menus = [SimpleNamespace(permission=permission) for permission in permissions]
    role = SimpleNamespace(menus=[])

    bind_fresh_role_permissions(role, menus)

    assert permissions <= {menu.permission for menu in role.menus}


@pytest.mark.parametrize(
    "payload",
    [
        {"parentId": "11"},
        {"ancestors": "0,11"},
        {"deptName": "Sales", "unexpected": True},
        {},
    ],
)
def test_base_department_update_rejects_structure_extra_and_empty_payloads(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        dept_schemas.DeptUpdate.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"deptName": None},
        {"orderNum": None},
        {"status": None},
    ],
)
def test_base_department_update_rejects_null_for_required_model_fields(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        dept_schemas.DeptUpdate.model_validate(payload)


def test_department_move_body_uses_one_canonical_string_or_null() -> None:
    schema = getattr(dept_schemas, "DeptMove", None)

    assert schema is not None
    assert schema.model_validate({"newParentId": "11"}).new_parent_id == 11
    assert schema.model_validate({"newParentId": None}).new_parent_id is None
    for invalid in (
        {"newParentId": 11},
        {"newParentId": "01"},
        {"newParentId": "0"},
        {"newParentId": True},
        {"newParentId": "11", "extra": 1},
    ):
        with pytest.raises(ValidationError):
            schema.model_validate(invalid)


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/tree", "GET"),
        ("/tree-option", "GET"),
        ("/tree-list", "GET"),
        ("/list", "GET"),
        ("/{dept_id}", "GET"),
    ],
)
def test_every_traditional_department_read_requires_list_permission(
    path: str,
    method: str,
) -> None:
    assert "system:dept:list" in _permission_codes(_route(path, method))


@pytest.mark.parametrize(
    ("path", "method", "permissions"),
    [
        ("/add", "POST", {"system:dept:add", "system:dept:list"}),
        ("/{dept_id}", "PUT", {"system:dept:edit", "system:dept:list"}),
        (
            "/{dept_id}/move",
            "PUT",
            {"system:dept:move", "system:dept:list"},
        ),
    ],
)
def test_department_writers_expose_the_shared_policy_permission_boundary(
    path: str,
    method: str,
    permissions: set[str],
) -> None:
    assert permissions <= _permission_codes(_route(path, method))
