"""Plan 5-B-C platform AI control-plane contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import text
from tenant_helpers import create_test_tenant

from app.core.exceptions import AuthorizationException, BusinessRuleException
from app.core.security import encrypt_value
from app.core.tenant import PlatformContext
from app.core.tenant_isolation_audit import (
    _schema_contract_issues,
    build_tenant_isolation_report,
)
from app.main import app
from app.middleware.audit_middleware import EXCLUDED_PATHS
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.model_policy import TenantAiModelPolicy
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.schemas.provider import ProviderOut
from app.modules.ai.service.model_service import model_service
from app.modules.ai.service.provider_service import provider_service
from app.modules.ai.service.tenant_model_policy_admin_service import (
    tenant_model_policy_admin_service,
)
from app.modules.platform.constants import (
    PLATFORM_AI_READ,
    PLATFORM_AI_WRITE,
    platform_permission_for_request,
)
from app.modules.platform.schemas import PlatformTenantModelPolicyPut


def _platform(*permissions: str, tenant_id: int | None = None) -> PlatformContext:
    return PlatformContext(
        actor_principal_id=7001,
        actor_name="platform-ai-operator",
        principal_type="human",
        permissions=frozenset(permissions),
        reason="Manage tenant AI policy",
        ticket_id="PLAN5BC-TEST",
        correlation_id="plan5bc-platform-ai",
        target_tenant_id=tenant_id,
    )


def _registered_paths() -> set[tuple[str, str]]:
    return {
        (route.path, method)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }


def test_platform_ai_routes_replace_legacy_tenant_namespace() -> None:
    routes = _registered_paths()
    assert ("/platform/ai/agents", "GET") in routes
    assert ("/platform/ai/providers", "GET") in routes
    assert (
        "/platform/tenants/{tenant_id}/ai/model-policies/{model_id}",
        "PUT",
    ) in routes
    assert ("/ai/admin/agents", "GET") not in routes
    assert ("/ai/provider/list", "GET") not in routes


def test_platform_control_plane_is_excluded_from_tenant_operation_audit() -> None:
    assert any("/platform/ai/providers".startswith(path) for path in EXCLUDED_PATHS)


@pytest.mark.parametrize(
    ("method", "path", "permission"),
    [
        ("GET", "/platform/ai/agents", PLATFORM_AI_READ),
        ("PUT", "/platform/ai/agents/{agent_id}", PLATFORM_AI_WRITE),
        ("GET", "/platform/ai/providers", PLATFORM_AI_READ),
        ("POST", "/platform/ai/providers", PLATFORM_AI_WRITE),
        (
            "GET",
            "/platform/tenants/{tenant_id}/ai/model-policies",
            PLATFORM_AI_READ,
        ),
        (
            "DELETE",
            "/platform/tenants/{tenant_id}/ai/model-policies/{model_id}",
            PLATFORM_AI_WRITE,
        ),
    ],
)
def test_platform_ai_routes_have_explicit_permission_mapping(
    method: str, path: str, permission: str
) -> None:
    assert platform_permission_for_request(method, path) == permission


def test_provider_projection_never_returns_api_key_fragments() -> None:
    provider = AiProvider(
        provider_id=7002,
        provider_code="safe-provider",
        name="Safe Provider",
        api_key=encrypt_value("sk-plan5bc-top-secret"),
        base_url="https://user:legacy-secret@example.test/v1?token=secret",
        is_enabled=True,
        config={"nested": {"apiToken": "config-secret", "temperature": 0.2}},
        create_time=datetime.now(UTC),
    )
    projection = ProviderOut.from_record(provider).model_dump(by_alias=True)

    assert projection["credentialConfigured"] is True
    assert "apiKey" not in projection
    assert "top-secret" not in str(projection)
    assert "legacy-secret" not in str(projection)
    assert "config-secret" not in str(projection)
    assert projection["baseUrl"] == "https://example.test/v1"
    assert projection["config"]["nested"]["apiToken"] == "***"


async def _provider_with_models(db_session) -> tuple[AiProvider, AiModel, AiModel]:
    marker = uuid4().hex[:12]
    provider = AiProvider(
        provider_code=f"plan5bc_{marker}",
        name=f"Plan 5-B-C {marker}",
        api_key=encrypt_value("test-secret"),
        base_url="https://api.openai.com/v1",
        is_enabled=True,
    )
    db_session.add(provider)
    await db_session.flush()
    first = AiModel(
        provider_id=provider.provider_id,
        name=f"first-{marker}",
        capabilities=["text"],
        is_enabled=True,
    )
    second = AiModel(
        provider_id=provider.provider_id,
        name=f"second-{marker}",
        capabilities=["text"],
        is_enabled=True,
    )
    db_session.add_all([first, second])
    await db_session.flush()
    return provider, first, second


async def test_tenant_model_policy_default_switch_is_target_scoped(db_session) -> None:
    tenant_a = await create_test_tenant(db_session, prefix="plan5bc-a")
    tenant_b = await create_test_tenant(db_session, prefix="plan5bc-b")
    _provider, first, second = await _provider_with_models(db_session)
    db_session.add_all(
        [
            TenantAiModelPolicy(
                tenant_id=tenant_a.tenant_id,
                model_id=first.model_id,
                enabled=True,
                is_default=True,
            ),
            TenantAiModelPolicy(
                tenant_id=tenant_b.tenant_id,
                model_id=first.model_id,
                enabled=True,
                is_default=True,
            ),
        ]
    )
    await db_session.flush()

    result = await tenant_model_policy_admin_service.put(
        db_session,
        tenant_id=tenant_a.tenant_id,
        model_id=second.model_id,
        data=PlatformTenantModelPolicyPut(
            enabled=True,
            is_default=True,
            daily_quota_per_user=25,
        ),
        platform=_platform(PLATFORM_AI_WRITE, tenant_id=tenant_a.tenant_id),
    )
    await db_session.flush()

    assert result.model_id == second.model_id
    assert result.is_default is True
    policies = (
        (
            await db_session.execute(
                TenantAiModelPolicy.__table__.select().order_by(
                    TenantAiModelPolicy.tenant_id,
                    TenantAiModelPolicy.model_id,
                )
            )
        )
        .mappings()
        .all()
    )
    by_key = {
        (row["tenant_id"], row["model_id"]): row["is_default"] for row in policies
    }
    assert by_key[(tenant_a.tenant_id, first.model_id)] is False
    assert by_key[(tenant_a.tenant_id, second.model_id)] is True
    assert by_key[(tenant_b.tenant_id, first.model_id)] is True


async def test_tenant_model_policy_rejects_cross_target_and_disabled_model(
    db_session,
) -> None:
    tenant_a = await create_test_tenant(db_session, prefix="p5bc-ta")
    tenant_b = await create_test_tenant(db_session, prefix="p5bc-tb")
    _provider, model, _second = await _provider_with_models(db_session)
    payload = PlatformTenantModelPolicyPut(enabled=True, is_default=False)

    with pytest.raises(AuthorizationException) as cross_target:
        await tenant_model_policy_admin_service.put(
            db_session,
            tenant_id=tenant_b.tenant_id,
            model_id=model.model_id,
            data=payload,
            platform=_platform(PLATFORM_AI_WRITE, tenant_id=tenant_a.tenant_id),
        )
    assert cross_target.value.error_code == "PLATFORM_TARGET_TENANT_MISMATCH"

    model.is_enabled = False
    await db_session.flush()
    with pytest.raises(BusinessRuleException) as unavailable:
        await tenant_model_policy_admin_service.put(
            db_session,
            tenant_id=tenant_a.tenant_id,
            model_id=model.model_id,
            data=payload,
            platform=_platform(PLATFORM_AI_WRITE, tenant_id=tenant_a.tenant_id),
        )
    assert unavailable.value.error_code == "PLATFORM_TENANT_MODEL_UNAVAILABLE"


async def test_global_provider_and_model_delete_reject_tenant_policy_references(
    db_session,
) -> None:
    tenant = await create_test_tenant(db_session, prefix="p5bc-delete")
    provider, model, _second = await _provider_with_models(db_session)
    db_session.add(
        TenantAiModelPolicy(
            tenant_id=tenant.tenant_id,
            model_id=model.model_id,
            enabled=True,
            is_default=True,
        )
    )
    await db_session.flush()
    platform = _platform(PLATFORM_AI_WRITE, tenant_id=tenant.tenant_id)

    with pytest.raises(BusinessRuleException) as model_in_use:
        await model_service.delete(db_session, model.model_id, platform=platform)
    assert model_in_use.value.error_code == "AI_MODEL_IN_USE_BY_TENANT_POLICY"

    with pytest.raises(BusinessRuleException) as provider_in_use:
        await provider_service.delete(
            db_session, provider.provider_id, platform=platform
        )
    assert provider_in_use.value.error_code == "AI_PROVIDER_IN_USE_BY_TENANT_POLICY"
    assert await db_session.get(AiModel, model.model_id) is not None
    assert await db_session.get(AiProvider, provider.provider_id) is not None


def test_tenant_model_policy_request_is_strict_and_default_must_be_enabled() -> None:
    with pytest.raises(ValueError):
        PlatformTenantModelPolicyPut.model_validate(
            {"enabled": True, "isDefault": False, "tenantId": "9"}
        )
    with pytest.raises(ValueError):
        PlatformTenantModelPolicyPut(enabled=False, is_default=True)


def test_schema_contract_rejects_noop_security_trigger_definition() -> None:
    schema = {
        "tables": {},
        "triggers": [
            {
                "table": "sys_platform_principal",
                "name": "trg_platform_principal_security_version",
                "definition": "CREATE TRIGGER ...",
                "function": "bump_platform_principal_security_version",
                "functionDefinition": "BEGIN RETURN NEW; END",
            },
            {
                "table": "sys_platform_audit_log",
                "name": "trg_platform_audit_validate_lineage",
                "definition": "CREATE TRIGGER ...",
                "function": "validate_platform_audit_lineage",
                "functionDefinition": "BEGIN RETURN NEW; END",
            },
            {
                "table": "sys_platform_audit_log",
                "name": "trg_platform_audit_append_only",
                "definition": "CREATE TRIGGER ...",
                "function": "reject_platform_audit_mutation",
                "functionDefinition": "BEGIN RETURN NEW; END",
            },
            {
                "table": "sys_tenant",
                "name": "trg_sys_tenant_security_version",
                "definition": "CREATE TRIGGER ...",
                "function": "bump_sys_tenant_security_version",
                "functionDefinition": "BEGIN RETURN NEW; END",
            },
        ],
    }

    issues = _schema_contract_issues(schema)

    assert {issue.code for issue in issues} == {"TENANT_SCHEMA_TRIGGER_MISMATCH"}


async def test_audit_report_is_canonical_secret_free_and_matches_current_db(
    db_session,
) -> None:
    report = await build_tenant_isolation_report(
        db_session,
        build_sha="abcdef1234567890",
    )
    rendered = json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True)
    assert report.risk_count == 0, report.payload["risks"]
    assert report.payload["buildSha"] == "abcdef1234567890"
    assert report.payload["modelInventory"]["tenantOwnedCount"] > 0
    assert report.payload["databaseChecks"]["legacyScopedMatch"] is True
    assert len(report.report_sha256) == 64
    assert "api_key" not in rendered.lower()
    assert "password" not in rendered.lower()


async def test_audit_report_rejects_any_unclassified_database_table(db_session) -> None:
    await db_session.execute(
        text("CREATE TABLE plan5_unclassified_resource (id BIGINT PRIMARY KEY)")
    )

    report = await build_tenant_isolation_report(
        db_session,
        build_sha="abcdef1234567890",
    )

    assert any(
        issue["code"] == "TENANT_MODEL_UNCLASSIFIED"
        and issue["resource"] == "plan5_unclassified_resource"
        for issue in report.payload["risks"]
    )


async def test_audit_report_rejects_missing_security_trigger(db_session) -> None:
    await db_session.execute(
        text("DROP TRIGGER trg_platform_audit_append_only ON sys_platform_audit_log")
    )

    report = await build_tenant_isolation_report(
        db_session,
        build_sha="abcdef1234567890",
    )

    assert any(
        issue["code"] == "TENANT_SCHEMA_TRIGGER_MISSING"
        and issue["resource"] == "sys_platform_audit_log.trg_platform_audit_append_only"
        for issue in report.payload["risks"]
    )
