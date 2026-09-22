from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.exceptions import AuthorizationException
from app.core.rbac import is_super_admin, is_system_admin
from app.db.session import get_db
from app.main import app
from app.modules.ai.service.agent_admin import agent_admin_service
from app.modules.auth.service import get_current_user
from app.modules.platform import system_agent_auth as module
from app.modules.platform.system_agent_auth import require_system_agent_context


@pytest.mark.parametrize(
    "tenant,name,status,role_status,expected",
    [
        (0, "admin", "1", "1", True),
        (1, "admin", "1", "1", False),
        (0, "renamed-admin", "1", "1", True),
        (0, "admin", "2", "1", False),
        (0, "admin", "1", "2", False),
    ],
)
def test_only_default_enabled_super_role_is_system_admin(
    tenant, name, status, role_status, expected
):
    actor = SimpleNamespace(
        tenant_id=tenant,
        user_name=name,
        status=status,
        roles=[
            SimpleNamespace(tenant_id=tenant, role_code="R_SUPER", status=role_status)
        ],
    )
    assert is_system_admin(actor) is expected


def test_admin_username_without_role_has_no_bypass():
    actor = SimpleNamespace(tenant_id=0, user_name="admin", status="1", roles=[])
    assert not is_system_admin(actor)
    assert not is_super_admin(actor)


async def test_tenant_admin_cannot_build_global_agent_authority():
    actor = SimpleNamespace(
        tenant_id=9, user_id=12, user_name="admin", status="1", roles=[]
    )
    request = SimpleNamespace(state=SimpleNamespace())
    dependency = require_system_agent_context(request, actor, AsyncMock())
    with pytest.raises(AuthorizationException) as caught:
        await anext(dependency)
    assert caught.value.error_code == "SYSTEM_ADMIN_ONLY"


async def test_system_admin_uses_existing_user_and_audits_without_second_identity(
    monkeypatch,
):
    persist = AsyncMock(return_value=101)
    monkeypatch.setattr(module, "persist_system_agent_audit", persist)
    actor = SimpleNamespace(
        tenant_id=0,
        user_id=12,
        user_name="admin",
        status="1",
        roles=[SimpleNamespace(tenant_id=0, role_code="R_SUPER", status="1")],
    )
    request = SimpleNamespace(
        state=SimpleNamespace(),
        scope={"route": SimpleNamespace(path="/platform/ai/agents")},
        url=SimpleNamespace(path="/platform/ai/agents"),
        method="GET",
        headers={
            "X-Platform-Reason": "Review configuration",
            "X-Platform-Ticket": "T-1",
            "X-Correlation-ID": "C-1",
        },
        client=None,
        query_params={},
    )
    db = AsyncMock()
    db.add = Mock()
    dependency = require_system_agent_context(request, actor, db)
    context = await anext(dependency)
    assert context.permissions == frozenset({"platform:ai:read", "platform:ai:write"})
    assert context.actor_name == "admin"
    assert persist.await_args.kwargs["user_id"] == 12
    with pytest.raises(StopAsyncIteration):
        await anext(dependency)
    db.add.assert_called_once()
    assert db.add.call_args.args[0].audit_scope == "platform"
    assert db.add.call_args.args[0].user_id == 12


@pytest.mark.parametrize(
    "tenant,name,role_status,expected",
    [(0, "renamed-admin", "1", 200), (0, "admin", "2", 403), (12, "admin", "1", 403)],
)
async def test_agent_http_authorizes_roles_not_names(
    client, monkeypatch, tenant, name, role_status, expected
):
    actor = SimpleNamespace(
        tenant_id=tenant,
        user_id=123,
        user_name=name,
        status="1",
        roles=[
            SimpleNamespace(tenant_id=tenant, role_code="R_SUPER", status=role_status)
        ],
    )
    db = AsyncMock()
    db.add = Mock()
    persist = AsyncMock(return_value=101)
    business = AsyncMock(return_value=[])
    monkeypatch.setattr(module, "persist_system_agent_audit", persist)
    monkeypatch.setattr(agent_admin_service, "list_agents", business)
    app.dependency_overrides[get_current_user] = lambda: actor
    app.dependency_overrides[get_db] = lambda: db
    try:
        response = await client.get(
            "/platform/ai/agents",
            headers={
                "X-Platform-Reason": "Review",
                "X-Platform-Ticket": "T-1",
                "X-Correlation-ID": "C-1",
            },
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == expected
    if expected == 200:
        business.assert_awaited_once()
        assert db.add.call_args.args[0].user_id == 123
    else:
        business.assert_not_awaited()
        assert response.json()["errorCode"] == "SYSTEM_ADMIN_ONLY"
