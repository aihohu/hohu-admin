"""Multi-model authorization preserves tenant scope and atomic updates."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select
from tenant_helpers import create_test_tenant

from app.core.exceptions import AuthorizationException, BusinessRuleException
from app.db.session import AsyncSessionLocal, engine, get_db
from app.main import app
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.model_policy import TenantAiModelPolicy
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.service.tenant_model_policy_admin_service import (
    TenantModelCatalog,
)
from app.modules.ai.service.tenant_model_policy_admin_service import (
    tenant_model_policy_admin_service as service,
)
from app.modules.auth.service import get_current_user
from app.modules.platform.constants import PLATFORM_AI_READ, PLATFORM_AI_WRITE
from app.modules.platform.schemas import (
    PlatformTenantModelCatalogOut,
    PlatformTenantModelPoliciesPut,
)
from app.modules.platform.system_agent_auth import require_system_agent_context
from app.modules.system.models.tenant import Tenant
from tests.modules.platform.test_platform_ai_control import (
    _platform,
    _provider_with_models,
)


def payload(catalog, enabled_ids=(), default_id=None):
    return PlatformTenantModelPoliciesPut(
        revision=catalog.revision,
        policies=[
            {
                "modelId": str(row.model_id),
                "enabled": row.model_id in enabled_ids,
                "isDefault": row.model_id == default_id,
                "dailyQuotaPerUser": 25 if row.model_id in enabled_ids else None,
            }
            for row in catalog.models
        ],
    )


async def setup_catalog(db):
    tenant = await create_test_tenant(db, prefix="batch")
    provider, first, second = await _provider_with_models(db)
    context = _platform(PLATFORM_AI_READ, PLATFORM_AI_WRITE, tenant_id=tenant.tenant_id)
    catalog = await service.catalog(db, tenant_id=tenant.tenant_id, platform=context)
    return tenant.tenant_id, context, provider, first, second, catalog


async def test_batch_saves_multiple_models_switches_default_and_rejects_stale(
    db_session,
):
    tid, ctx, _provider, first, second, catalog = await setup_catalog(db_session)
    data = payload(catalog, [first.model_id, second.model_id], second.model_id)
    await service.put_many(db_session, tenant_id=tid, data=data, platform=ctx)
    rows = await service.list(db_session, tenant_id=tid, platform=ctx)
    assert {row.model_id for row in rows if row.enabled} == {
        first.model_id,
        second.model_id,
    }
    assert [row.model_id for row in rows if row.is_default] == [second.model_id]
    with pytest.raises(BusinessRuleException, match=".*") as stale:
        await service.put_many(db_session, tenant_id=tid, data=data, platform=ctx)
    assert stale.value.error_code == "PLATFORM_TENANT_MODEL_POLICIES_STALE"
    fresh = await service.catalog(db_session, tenant_id=tid, platform=ctx)
    await service.put_many(
        db_session,
        tenant_id=tid,
        data=payload(fresh, [first.model_id], first.model_id),
        platform=ctx,
    )
    rows = await service.list(db_session, tenant_id=tid, platform=ctx)
    assert [row.model_id for row in rows if row.is_default] == [first.model_id]


async def test_batch_rejects_unavailable_model_without_partial_writes(db_session):
    tid, ctx, _provider, first, second, _catalog = await setup_catalog(db_session)
    second.is_enabled = False
    await db_session.flush()
    catalog = await service.catalog(db_session, tenant_id=tid, platform=ctx)
    assert (
        next(
            row for row in catalog.models if row.model_id == second.model_id
        ).unavailable_reason
        == "model_disabled"
    )
    with pytest.raises(BusinessRuleException) as error:
        await service.put_many(
            db_session,
            tenant_id=tid,
            data=payload(catalog, [first.model_id, second.model_id], first.model_id),
            platform=ctx,
        )
    assert error.value.error_code == "PLATFORM_TENANT_MODEL_UNAVAILABLE"
    assert not (
        await db_session.scalars(
            select(TenantAiModelPolicy).where(TenantAiModelPolicy.tenant_id == tid)
        )
    ).all()


async def test_batch_checks_scope_permission_and_all_off(db_session):
    tid, ctx, _provider, first, _second, catalog = await setup_catalog(db_session)
    data = payload(catalog, [first.model_id], first.model_id)
    for wrong in (
        replace(ctx, target_tenant_id=0),
        replace(ctx, permissions=frozenset({PLATFORM_AI_READ})),
    ):
        with pytest.raises(AuthorizationException):
            await service.put_many(db_session, tenant_id=tid, data=data, platform=wrong)
    await service.put_many(db_session, tenant_id=tid, data=data, platform=ctx)
    fresh = await service.catalog(db_session, tenant_id=tid, platform=ctx)
    await service.put_many(db_session, tenant_id=tid, data=payload(fresh), platform=ctx)
    assert all(
        not row.enabled and not row.is_default
        for row in await service.list(db_session, tenant_id=tid, platform=ctx)
    )


@pytest.mark.parametrize(
    "policies",
    [
        [{"modelId": "1", "enabled": True, "isDefault": False}],
        [{"modelId": "1", "enabled": True, "isDefault": True}] * 2,
        [{"modelId": str(i), "enabled": True, "isDefault": True} for i in (1, 2)],
        [{"modelId": "1", "enabled": False, "isDefault": True}],
        [{"modelId": "1", "enabled": True, "isDefault": True, "dailyQuotaPerUser": 0}],
    ],
)
def test_batch_rejects_ambiguous_or_invalid_policies(policies):
    with pytest.raises(ValueError):
        PlatformTenantModelPoliciesPut(revision="a" * 64, policies=policies)


@pytest.mark.parametrize(
    "model_id", [9007199254740993, 1.5, True, "0", "9223372036854775808"]
)
def test_batch_requires_lossless_positive_string_ids(model_id):
    with pytest.raises(ValueError):
        PlatformTenantModelPoliciesPut(
            revision="a" * 64,
            policies=[{"modelId": model_id, "enabled": False, "isDefault": False}],
        )


def test_batch_openapi_declares_string_model_ids():
    schema = PlatformTenantModelPoliciesPut.model_json_schema(by_alias=True)
    assert (
        schema["$defs"]["PlatformTenantModelPolicyItem"]["properties"]["modelId"][
            "type"
        ]
        == "string"
    )


async def test_batch_rolls_back_changes_when_database_flush_fails(
    db_session, monkeypatch
):
    tid, ctx, _provider, first, second, catalog = await setup_catalog(db_session)
    await service.put_many(
        db_session,
        tenant_id=tid,
        data=payload(catalog, [first.model_id], first.model_id),
        platform=ctx,
    )
    before = await service.catalog(db_session, tenant_id=tid, platform=ctx)
    original_flush = db_session.flush

    async def fail_after_writes(*args, **kwargs):
        if db_session.new or db_session.dirty:
            raise RuntimeError("simulated database write failure")
        return await original_flush(*args, **kwargs)

    monkeypatch.setattr(db_session, "flush", fail_after_writes)
    with pytest.raises(RuntimeError, match="simulated"):
        await service.put_many(
            db_session,
            tenant_id=tid,
            data=payload(before, [second.model_id], second.model_id),
            platform=ctx,
        )
    monkeypatch.setattr(db_session, "flush", original_flush)
    after = await service.catalog(db_session, tenant_id=tid, platform=ctx)
    assert after.revision == before.revision


async def test_batch_preserves_unavailable_grant_and_can_move_its_default(db_session):
    tid, ctx, _provider, first, second, catalog = await setup_catalog(db_session)
    await service.put_many(
        db_session,
        tenant_id=tid,
        data=payload(catalog, [first.model_id], first.model_id),
        platform=ctx,
    )
    first.is_enabled = False
    await db_session.flush()
    fresh = await service.catalog(db_session, tenant_id=tid, platform=ctx)
    await service.put_many(
        db_session,
        tenant_id=tid,
        data=payload(fresh, [first.model_id, second.model_id], second.model_id),
        platform=ctx,
    )
    rows = await service.list(db_session, tenant_id=tid, platform=ctx)
    assert [row.model_id for row in rows if row.is_default] == [second.model_id]


async def test_catalog_serializes_ids_and_invalidates_when_model_changes(db_session):
    tid, ctx, _provider, first, _second, catalog = await setup_catalog(db_session)
    response = PlatformTenantModelCatalogOut.from_projection(catalog).model_dump(
        mode="json", by_alias=True
    )
    assert all(
        isinstance(row["modelId"], str) and isinstance(row["providerId"], str)
        for row in response["models"]
    )
    first.capabilities = ["image"]
    await db_session.flush()
    with pytest.raises(BusinessRuleException) as error:
        await service.put_many(
            db_session, tenant_id=tid, data=payload(catalog), platform=ctx
        )
    assert error.value.error_code == "PLATFORM_TENANT_MODEL_POLICIES_STALE"


async def test_concurrent_batch_saves_allow_only_one_snapshot_writer():
    tid = provider_id = None
    try:
        async with AsyncSessionLocal() as setup:
            tid, ctx, provider, first, second, catalog = await setup_catalog(setup)
            provider_id = provider.provider_id
            first_data = payload(catalog, [first.model_id], first.model_id)
            second_data = payload(catalog, [second.model_id], second.model_id)
            await setup.commit()

        async def save_once(data):
            async with AsyncSessionLocal() as session:
                try:
                    await service.put_many(
                        session, tenant_id=tid, data=data, platform=ctx
                    )
                    await session.commit()
                    return "saved"
                except BusinessRuleException as error:
                    await session.rollback()
                    return error.error_code

        outcomes = await asyncio.wait_for(
            asyncio.gather(save_once(first_data), save_once(second_data)), timeout=15
        )
        assert sorted(outcomes) == ["PLATFORM_TENANT_MODEL_POLICIES_STALE", "saved"]
    finally:
        async with AsyncSessionLocal() as cleanup:
            if tid is not None:
                await cleanup.execute(
                    delete(TenantAiModelPolicy).where(
                        TenantAiModelPolicy.tenant_id == tid
                    )
                )
                await cleanup.execute(delete(Tenant).where(Tenant.tenant_id == tid))
            if provider_id is not None:
                await cleanup.execute(
                    delete(AiModel).where(AiModel.provider_id == provider_id)
                )
                await cleanup.execute(
                    delete(AiProvider).where(AiProvider.provider_id == provider_id)
                )
            await cleanup.commit()
        await engine.dispose()


async def test_batch_http_uses_transaction_and_string_id_response(client, monkeypatch):
    db = AsyncMock()
    ctx = _platform(PLATFORM_AI_WRITE, tenant_id=0)
    business = AsyncMock(return_value=TenantModelCatalog([], "a" * 64))
    monkeypatch.setattr(service, "put_many", business)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_system_agent_context] = lambda: ctx
    try:
        response = await client.put(
            "/platform/tenants/0/ai/model-policies",
            json={"revision": "a" * 64, "policies": []},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_system_agent_context, None)
    assert response.status_code == 200
    assert response.json()["data"] == {"models": [], "revision": "a" * 64}
    db.commit.assert_awaited_once()
    assert business.await_args.kwargs["tenant_id"] == 0


@pytest.mark.parametrize("method,path", [("GET", "/catalog"), ("PUT", "")])
async def test_batch_http_denies_tenant_admin(client, method, path):
    actor = SimpleNamespace(
        user_id=123,
        tenant_id=7,
        user_name="admin",
        status="1",
        roles=[SimpleNamespace(tenant_id=7, role_code="R_SUPER", status="1")],
    )
    app.dependency_overrides[get_current_user] = lambda: actor
    try:
        response = await client.request(
            method,
            f"/platform/tenants/0/ai/model-policies{path}",
            json={"revision": "a" * 64, "policies": []},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 403
    assert response.json()["errorCode"] == "SYSTEM_ADMIN_ONLY"


async def test_batch_never_changes_other_tenant_grants(db_session):
    tid, ctx, _provider, first, second, catalog = await setup_catalog(db_session)
    other = await create_test_tenant(db_session, prefix="batch-other")
    db_session.add(
        TenantAiModelPolicy(
            tenant_id=other.tenant_id,
            model_id=first.model_id,
            enabled=True,
            is_default=True,
        )
    )
    await db_session.flush()
    await service.put_many(
        db_session,
        tenant_id=tid,
        data=payload(catalog, [second.model_id], second.model_id),
        platform=ctx,
    )
    unchanged = await db_session.get(
        TenantAiModelPolicy, (other.tenant_id, first.model_id)
    )
    assert unchanged.enabled and unchanged.is_default
