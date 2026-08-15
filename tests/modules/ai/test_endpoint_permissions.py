"""AI endpoint 的入口权限矩阵测试。"""

from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute

from app.constants import STATUS_ENABLED
from app.core.auth import (
    AI_CHAT_USE_PERMISSION,
    ensure_ai_chat_use,
    require_ai_chat_use,
)
from app.core.exceptions import AuthorizationException
from app.modules.ai.api.agent import admin_router as agent_admin_router
from app.modules.ai.api.agent import router as agent_router
from app.modules.ai.api.chat import router as chat_router
from app.modules.ai.api.confirm import router as confirm_router
from app.modules.ai.api.conversation import router as conversation_router
from app.modules.ai.api.operation_log import router as operation_log_router
from app.modules.ai.api.provider import router as provider_router
from app.modules.ai.api.query_cache import router as query_cache_router
from app.modules.ai.api.resume import router as resume_router
from app.modules.ai.api.role_agent import router as role_agent_router
from app.modules.ai.api.routing_feedback import query_router as feedback_query_router
from app.modules.ai.api.routing_feedback import router as feedback_router


def _user(*permissions: str, role_code: str = "R_USER"):
    menus = [SimpleNamespace(permission=permission) for permission in permissions]
    role = SimpleNamespace(
        role_code=role_code,
        status=STATUS_ENABLED,
        menus=menus,
    )
    return SimpleNamespace(user_id=100, user_name="alice", roles=[role])


def _route(router, path: str, method: str) -> APIRoute:  # noqa: ANN001
    matches = [
        route
        for route in router.routes
        if isinstance(route, APIRoute)
        and route.path == path
        and method in route.methods
    ]
    assert len(matches) == 1, (path, method, matches)
    return matches[0]


def _dependency_calls(route: APIRoute) -> set[object]:
    calls: set[object] = set()

    def collect(dependant) -> None:  # noqa: ANN001
        for dependency in dependant.dependencies:
            calls.add(dependency.call)
            collect(dependency)

    collect(route.dependant)
    return calls


@pytest.mark.parametrize(
    ("router", "path", "method"),
    [
        (agent_router, "", "GET"),
        (chat_router, "", "POST"),
        (chat_router, "/models", "GET"),
        (conversation_router, "/list", "GET"),
        (conversation_router, "/{conversation_id}", "GET"),
        (conversation_router, "", "POST"),
        (conversation_router, "/{conversation_id}", "PUT"),
        (conversation_router, "/{conversation_id}", "DELETE"),
        (query_cache_router, "/{trace_id}", "GET"),
        (feedback_router, "/{message_id}/routing-feedback", "POST"),
    ],
)
def test_user_business_endpoints_require_ai_chat_use(
    router,
    path: str,
    method: str,  # noqa: ANN001
) -> None:
    route = _route(router, path, method)
    assert require_ai_chat_use in _dependency_calls(route)


@pytest.mark.parametrize(
    ("router", "path", "method"),
    [
        (confirm_router, "", "POST"),
        (resume_router, "/resume", "GET"),
        (operation_log_router, "", "GET"),
        (agent_admin_router, "", "GET"),
        (agent_admin_router, "/model-options", "GET"),
        (feedback_query_router, "/summary", "GET"),
        (feedback_query_router, "/list", "GET"),
        (provider_router, "/models", "GET"),
        (provider_router, "/list", "GET"),
        (role_agent_router, "/{role_id}", "GET"),
        (role_agent_router, "/{role_id}", "PUT"),
    ],
)
def test_branch_and_admin_endpoints_do_not_apply_chat_permission_globally(
    router,
    path: str,
    method: str,  # noqa: ANN001
) -> None:
    route = _route(router, path, method)
    assert require_ai_chat_use not in _dependency_calls(route)


def _permission_codes(route: APIRoute) -> set[str]:
    codes: set[str] = set()
    for call in _dependency_calls(route):
        for cell in getattr(call, "__closure__", None) or ():
            value = cell.cell_contents
            if isinstance(value, str):
                codes.add(value)
    return codes


def test_provider_model_management_endpoint_requires_provider_list() -> None:
    route = _route(provider_router, "/models", "GET")
    assert "ai:provider:list" in _permission_codes(route)


def test_agent_model_options_endpoint_requires_agent_list() -> None:
    route = _route(agent_admin_router, "/model-options", "GET")
    assert "ai:agent:list" in _permission_codes(route)


def test_ai_chat_permission_has_stable_denial_code() -> None:
    with pytest.raises(AuthorizationException) as exc_info:
        ensure_ai_chat_use(_user())
    assert exc_info.value.code == 403
    assert exc_info.value.error_code == "AI_CHAT_PERMISSION_DENIED"


def test_ai_chat_permission_requires_explicit_enabled_role_permission() -> None:
    ensure_ai_chat_use(_user(AI_CHAT_USE_PERMISSION))

    # R_SUPER 也通过 seed 显式获得入口权限，不保留特殊代码旁路。
    with pytest.raises(AuthorizationException):
        ensure_ai_chat_use(_user(role_code="R_SUPER"))
