"""Phase 3 strict Role Agent and page-writer contracts."""

import inspect
from unittest.mock import ANY, AsyncMock, patch

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy import select

from app.db.authorization_plan_session import AuthorizationPlanSession
from app.db.base import role_menus
from app.modules.system.api.role import router
from app.modules.system.schemas.role import RoleUpdate
from app.modules.system.service import role_management_service as role_policy_module
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_system_http import (
    http_system as http_system,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


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


async def test_role_list_uses_shared_delegation_summary_service(http_system):
    client, _ = http_system
    service = role_policy_module.role_management_service
    with patch.object(
        service, "summarize_roles", AsyncMock(wraps=service.summarize_roles)
    ) as summarize:
        response = await client.get("/role/list")
    assert response.status_code == 200, response.text
    summarize.assert_awaited_once()
    assert isinstance(summarize.await_args.args[0], AuthorizationPlanSession)
    assert response.json()["data"]["records"][0]["delegable"] is True


async def test_role_menu_read_rechecks_current_delegation_policy(http_system):
    client, plain = http_system
    async with plain() as db:
        expected = {
            str(mid)
            for mid in (
                await db.execute(
                    select(role_menus.c.menu_id).where(
                        role_menus.c.tenant_id == 0, role_menus.c.role_id == 201
                    )
                )
            ).scalars()
        }
    service = role_policy_module.role_management_service
    with patch.object(
        service,
        "authorize_role_projection",
        AsyncMock(wraps=service.authorize_role_projection),
    ) as authorize:
        response = await client.get("/role/menus/201")
    assert response.status_code == 200, response.text
    assert set(response.json()["data"]) == expected
    authorize.assert_awaited_once_with(
        ANY, actor_user_id=101, role_id=201, tenant=TENANT
    )
    assert isinstance(authorize.await_args.args[0], AuthorizationPlanSession)
