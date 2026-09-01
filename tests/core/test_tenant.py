from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from app.core.exceptions import AuthenticationException, AuthorizationException
from app.core.tenant import (
    TenantContext,
    bind_tenant_context,
    create_worker_envelope,
    resolve_tenant_id,
    revalidate_worker_envelope,
)
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
