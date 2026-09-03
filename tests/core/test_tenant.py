from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import AuthenticationException, AuthorizationException
from app.core.tenant import (
    PlatformContext,
    TenantContext,
    bind_tenant_context,
    create_worker_envelope,
    require_platform_permission,
    resolve_tenant_id,
    revalidate_worker_envelope,
)
from app.modules.ai.lifecycle import _tenant_context
from app.modules.system.models.user import User


def test_tenant_context_is_frozen_and_is_the_primary_tenant_source():
    tenant = TenantContext(
        tenant_id=7,
        tenant_code="acme",
        actor_user_id=101,
        tenant_version=3,
        source="access_token",
    )

    assert resolve_tenant_id(tenant) == 7
    with pytest.raises(FrozenInstanceError):
        tenant.tenant_id = 8  # type: ignore[misc]


def test_tenant_context_rejects_an_unknown_authority_source():
    with pytest.raises(ValueError, match="tenant source"):
        TenantContext(
            tenant_id=7,
            tenant_code="acme",
            actor_user_id=101,
            tenant_version=1,
            source="client_payload",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("actor_principal_id", "principal_type", "reason", "ticket_id", "correlation_id"),
    [
        (True, "human", "maintenance", "OPS-1", "run-1"),
        (0, "human", "maintenance", "OPS-1", "run-1"),
        (-1, "service", "maintenance", "OPS-1", "run-1"),
        (1, "human", "   ", "OPS-1", "run-1"),
        (1, "human", "maintenance", "   ", "run-1"),
        (1, "human", "maintenance", "OPS-1", "   "),
    ],
)
def test_platform_context_rejects_invalid_audit_identity(
    actor_principal_id, principal_type, reason, ticket_id, correlation_id
):
    with pytest.raises(ValueError):
        PlatformContext(
            actor_principal_id=actor_principal_id,
            actor_name="platform-operator",
            principal_type=principal_type,
            permissions=frozenset({"platform:ai:read"}),
            reason=reason,
            ticket_id=ticket_id,
            correlation_id=correlation_id,
        )


def test_platform_context_enforces_exact_permissions():
    platform = PlatformContext(
        actor_principal_id=9,
        actor_name="platform-auditor",
        principal_type="human",
        permissions=frozenset({"platform:ai:read"}),
        reason="Review AI configuration",
        ticket_id="SEC-42",
        correlation_id="review-42",
    )

    require_platform_permission(platform, "platform:ai:read")
    with pytest.raises(AuthorizationException) as exc_info:
        require_platform_permission(platform, "platform:ai:write")

    assert exc_info.value.error_code == "PLATFORM_PERMISSION_DENIED"


async def test_platform_lifecycle_derives_an_explicit_control_plane_scope():
    platform = PlatformContext(
        actor_principal_id=0,
        actor_name="ai-lifecycle",
        principal_type="service",
        permissions=frozenset({"platform:ai:lifecycle"}),
        reason="AI lifecycle recovery",
        ticket_id="SYSTEM-LIFECYCLE",
        correlation_id="lifecycle-1",
    )
    db = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                tenant_id=37,
                tenant_code="tenant-37",
                row_version=4,
            )
        )
    )

    tenant = await _tenant_context(
        db,
        tenant_id=37,
        actor_user_id=101,
        platform=platform,
    )

    assert tenant.tenant_id == 37
    assert tenant.actor_user_id == 101
    assert tenant.source == "platform_control"


def test_resolver_rejects_client_shaped_objects_with_a_tenant_id_field():
    client_payload = SimpleNamespace(tenant_id=999, tenant_code="attacker")

    with pytest.raises(AuthenticationException) as exc_info:
        resolve_tenant_id(client_payload)

    assert exc_info.value.error_code == "TENANT_CONTEXT_REQUIRED"


def test_resolver_rejects_an_unbound_server_principal_during_m1():
    principal = User(user_id=101, tenant_id=7, user_name="alice", status="1")

    with pytest.raises(AuthenticationException) as exc_info:
        resolve_tenant_id(principal)

    assert exc_info.value.error_code == "TENANT_CONTEXT_REQUIRED"


def test_resolver_rejects_unbound_default_tenant_user_after_plan2():
    principal = User(user_id=101, tenant_id=0, user_name="alice", status="1")

    with pytest.raises(AuthenticationException) as exc_info:
        resolve_tenant_id(principal)

    assert exc_info.value.error_code == "TENANT_CONTEXT_REQUIRED"


def test_binding_rejects_actor_mismatch_and_accepts_matching_principal():
    principal = User(user_id=101, tenant_id=7, user_name="alice", status="1")
    mismatched = TenantContext(
        tenant_id=7,
        tenant_code="acme",
        actor_user_id=202,
        tenant_version=1,
        source="access_token",
    )

    with pytest.raises(AuthenticationException) as exc_info:
        bind_tenant_context(principal, mismatched)

    assert exc_info.value.error_code == "TENANT_CONTEXT_INVALID"

    matching = TenantContext(
        tenant_id=7,
        tenant_code="acme",
        actor_user_id=101,
        tenant_version=1,
        source="access_token",
    )
    bind_tenant_context(principal, matching)
    assert resolve_tenant_id(principal) == 7


def test_platform_derived_scope_cannot_be_laundered_into_user_or_worker_authority():
    principal = User(user_id=101, tenant_id=7, user_name="alice", status="1")
    platform_scope = TenantContext(
        tenant_id=7,
        tenant_code="acme",
        actor_user_id=101,
        tenant_version=1,
        source="platform_control",
    )

    with pytest.raises(AuthenticationException) as bound:
        bind_tenant_context(principal, platform_scope)
    assert bound.value.error_code == "TENANT_CONTEXT_INVALID"

    with pytest.raises(ValueError, match="platform-derived"):
        create_worker_envelope(
            platform_scope,
            job_id="job-platform-laundering",
            scope_hash="scope-v1",
            secret="test-secret",
        )


def test_worker_envelope_is_signed_and_revalidated_against_live_tenant():
    tenant = TenantContext(
        tenant_id=7,
        tenant_code="acme",
        actor_user_id=101,
        tenant_version=3,
        source="access_token",
    )
    envelope = create_worker_envelope(
        tenant,
        job_id="job-42",
        scope_hash="scope-v1",
        secret="test-secret",
    )
    live_tenant = SimpleNamespace(
        tenant_id=7,
        tenant_code="acme",
        status="1",
        row_version=3,
    )

    restored = revalidate_worker_envelope(
        envelope,
        live_tenant=live_tenant,
        secret="test-secret",
    )

    assert restored == TenantContext(
        tenant_id=7,
        tenant_code="acme",
        actor_user_id=101,
        tenant_version=3,
        source="worker_envelope",
    )


def test_worker_envelope_rejects_tampering_and_disabled_tenant():
    tenant = TenantContext(
        tenant_id=7,
        tenant_code="acme",
        actor_user_id=101,
        tenant_version=3,
        source="access_token",
    )
    envelope = create_worker_envelope(
        tenant,
        job_id="job-42",
        scope_hash="scope-v1",
        secret="test-secret",
    )

    with pytest.raises(AuthenticationException) as tampered:
        revalidate_worker_envelope(
            envelope,
            live_tenant=SimpleNamespace(
                tenant_id=8,
                tenant_code="other",
                status="1",
                row_version=3,
            ),
            secret="test-secret",
        )
    assert tampered.value.error_code == "TENANT_CONTEXT_INVALID"

    with pytest.raises(AuthorizationException) as disabled:
        revalidate_worker_envelope(
            envelope,
            live_tenant=SimpleNamespace(
                tenant_id=7,
                tenant_code="acme",
                status="2",
                row_version=3,
            ),
            secret="test-secret",
        )
    assert disabled.value.error_code == "TENANT_DISABLED"
