"""Phase 3 strict Role Agent and page-writer contracts."""

import inspect
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError

from app.modules.system.api.role import get_menus, list_roles, router
from app.modules.system.schemas.role import RoleQuery, RoleUpdate
from app.modules.system.service import role_management_service as role_policy_module


def _route(path: str, method: str) -> APIRoute:
    matches = [
        route
        for route in router.routes
        if isinstance(route, APIRoute)
        and route.path == path
        and method in route.methods
    ]
    assert len(matches) == 1
    return matches[0]


def _permission_codes(route: APIRoute) -> set[str]:
    codes: set[str] = set()

    def collect(dependant) -> None:  # noqa: ANN001
        for dependency in dependant.dependencies:
            if inspect.isfunction(dependency.call) or inspect.ismethod(dependency.call):
                permission = inspect.getclosurevars(dependency.call).nonlocals.get(
                    "perm_code"
                )
                if isinstance(permission, str):
                    codes.add(permission)
            collect(dependency)

    collect(route.dependant)
    return codes


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"roleCode": "R_RENAMED"},
        {"roleName": "Auditor", "unexpected": True},
        {"status": None},
        {"dataScope": None},
    ],
)
def test_role_update_rejects_empty_immutable_extra_and_null_fields(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        RoleUpdate.model_validate(payload)


def test_shared_role_policy_exposes_preview_and_snapshot_execution() -> None:
    service = getattr(role_policy_module, "role_management_service", None)

    assert service is not None
    for action in ("create", "update", "update_menus", "update_agents"):
        preview = getattr(service, f"preview_{action}", None)
        execute = getattr(service, action, None)
        assert preview is not None
        assert execute is not None
        assert "actor_user_id" in inspect.signature(preview).parameters
        assert "actor_user_id" in inspect.signature(execute).parameters
        assert "expected_snapshot" in inspect.signature(execute).parameters
        assert ".commit(" not in inspect.getsource(preview)
        assert ".commit(" not in inspect.getsource(execute)


@pytest.mark.parametrize(
    ("path", "method"),
    [("/list", "GET"), ("/all", "GET"), ("/{role_id}", "GET")],
)
def test_traditional_role_metadata_reads_require_list_permission(
    path: str,
    method: str,
) -> None:
    assert "system:role:list" in _permission_codes(_route(path, method))


def test_role_list_response_is_the_minimal_delegation_summary() -> None:
    schema = _route("/list", "GET").response_model.model_json_schema()
    serialized = str(schema)

    assert "delegable" in serialized
    assert "blockedReasonCode" in serialized
    assert "roleDesc" not in serialized
    assert "deptIds" not in serialized


async def test_role_list_uses_shared_delegation_summary_service() -> None:
    summary = SimpleNamespace(
        role_id=101,
        role_code="R_SCOPED",
        role_name="Scoped",
        status="1",
        data_scope="5",
        delegable=True,
        blocked_reason_code=None,
    )
    current_user = SimpleNamespace(user_id=202)
    with patch.object(
        role_policy_module.role_management_service,
        "summarize_roles",
        AsyncMock(return_value=([summary], 1, (101,))),
    ) as summarize:
        response = await list_roles(
            query=RoleQuery(current=1, size=10),
            db=AsyncMock(),
            _current_user=current_user,
        )

    summarize.assert_awaited_once()
    assert response.data.records[0].delegable is True


async def test_role_menu_read_rechecks_current_delegation_policy() -> None:
    current_user = SimpleNamespace(user_id=303)
    with (
        patch.object(
            role_policy_module.role_management_service,
            "authorize_role_projection",
            AsyncMock(),
            create=True,
        ) as authorize,
        patch(
            "app.modules.system.api.role.role_service.get_role_menus",
            AsyncMock(return_value=["1"]),
        ),
    ):
        await get_menus(
            role_id=101,
            db=AsyncMock(),
            _current_user=current_user,
        )

    authorize.assert_awaited_once_with(
        ANY,
        actor_user_id=current_user.user_id,
        role_id=101,
    )
