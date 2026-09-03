import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, select

from app.constants import SUPER_ADMIN_ROLE_CODE, USER_ROLE_CODE
from app.core.exceptions import AuthorizationException, BusinessException
from app.core.id_generator import next_id
from app.core.security import verify_password
from app.core.tenant import PlatformContext
from app.db.base import role_menus, user_roles
from app.db.session import AsyncSessionLocal, engine
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.model_policy import TenantAiModelPolicy
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.ai.service.tenant_bootstrap_service import ai_tenant_bootstrap_service
from app.modules.platform.constants import (
    PLATFORM_TENANT_BOOTSTRAP,
    PLATFORM_TENANT_WRITE,
)
from app.modules.platform.schemas import PlatformTenantBootstrapRequest
from app.modules.platform.tenant_bootstrap_service import tenant_bootstrap_service
from app.modules.system.hosted_menu_seed import HOSTED_PERMISSION_CODES
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User


def _platform(permission: str, tenant_id: int) -> PlatformContext:
    return PlatformContext(
        actor_principal_id=501,
        actor_name="tenant-bootstrapper",
        principal_type="human",
        permissions=frozenset({permission}),
        reason="Initialize a prepared customer tenant",
        ticket_id="TENANT-BOOTSTRAP-501",
        correlation_id=f"tenant-bootstrap:{tenant_id}",
        target_tenant_id=tenant_id,
    )


async def _prepared_tenant(db_session) -> Tenant:
    tenant_id = next_id()
    tenant = Tenant(
        tenant_id=tenant_id,
        tenant_code=f"bootstrap-{tenant_id}",
        tenant_name="Bootstrap Tenant",
        status="2",
        lifecycle_state="prepared",
        bootstrap_version=0,
        row_version=1,
    )
    db_session.add(tenant)
    await db_session.flush()
    return tenant


async def _text_model(db_session) -> AiModel:
    marker = str(next_id())
    provider = AiProvider(
        provider_code=f"bootstrap_{marker}",
        name="Bootstrap Provider",
        api_key="encrypted-test-key",
        base_url="https://api.openai.com/v1",
        is_enabled=True,
    )
    db_session.add(provider)
    await db_session.flush()
    model = AiModel(
        provider_id=provider.provider_id,
        name=f"bootstrap-model-{marker}",
        capabilities=["text"],
        is_enabled=True,
    )
    db_session.add(model)
    await db_session.flush()
    return model


async def test_bootstrap_prepared_tenant_seeds_only_hosted_capabilities(db_session):
    tenant = await _prepared_tenant(db_session)
    model = await _text_model(db_session)
    raw_password = "TenantAdmin123"
    mutable_default_home = await db_session.scalar(
        select(Menu).where(Menu.tenant_id == 0, Menu.route_name == "home")
    )
    assert mutable_default_home is not None
    mutable_default_home.component = "view.compromised_default_tenant"
    mutable_default_home.href = "https://phishing.invalid"
    await db_session.flush()

    result = await tenant_bootstrap_service.bootstrap(
        db_session,
        tenant_id=tenant.tenant_id,
        default_model_id=model.model_id,
        admin_password=raw_password,
        idempotency_key="tenant-bootstrap-idempotency-0001",
        platform=_platform(PLATFORM_TENANT_BOOTSTRAP, tenant.tenant_id),
    )

    await db_session.refresh(tenant)
    assert result.replayed is False
    assert result.admin_username == "admin"
    assert result.tenant_code == tenant.tenant_code
    assert result.menu_count > 0
    assert result.role_count == 2
    assert result.model_policy_count == 1
    assert tenant.status == "2"
    assert tenant.lifecycle_state == "prepared"
    assert tenant.bootstrap_version == 1
    assert tenant.row_version == 2
    assert tenant.bootstrap_key_hash != "tenant-bootstrap-idempotency-0001"
    assert tenant.bootstrap_fingerprint != raw_password

    users = (
        (
            await db_session.execute(
                select(User).where(User.tenant_id == tenant.tenant_id)
            )
        )
        .scalars()
        .all()
    )
    roles = (
        (
            await db_session.execute(
                select(Role).where(Role.tenant_id == tenant.tenant_id)
            )
        )
        .scalars()
        .all()
    )
    menus = (
        (
            await db_session.execute(
                select(Menu).where(Menu.tenant_id == tenant.tenant_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(users) == 1
    assert users[0].user_name == "admin"
    assert verify_password(raw_password, users[0].hashed_password)
    assert {role.role_code for role in roles} == {
        SUPER_ADMIN_ROLE_CODE,
        USER_ROLE_CODE,
    }
    assert {menu.route_name for menu in menus}.isdisjoint(
        {"ai_provider", "ai_agent", "marketplace", "lowcode"}
    )
    assert {menu.permission for menu in menus}.isdisjoint(
        {
            "ai:agent:list",
            "ai:agent:edit",
            "monitor:operation-log:delete",
            "monitor:operation-log:clean",
            "monitor:login-log:delete",
            "monitor:login-log:clean",
        }
    )
    hosted_home = next(menu for menu in menus if menu.route_name == "home")
    assert hosted_home.component == "layout.base$view.home"
    assert hosted_home.href is None
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(TenantAiModelPolicy)
            .where(TenantAiModelPolicy.tenant_id == tenant.tenant_id)
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(user_roles)
            .where(user_roles.c.tenant_id == tenant.tenant_id)
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(role_menus)
            .where(role_menus.c.tenant_id == tenant.tenant_id)
        )
        > 0
    )
    super_role = next(role for role in roles if role.role_code == SUPER_ADMIN_ROLE_CODE)
    assigned_permissions = set(
        (
            await db_session.execute(
                select(Menu.permission)
                .join(role_menus, role_menus.c.menu_id == Menu.menu_id)
                .where(
                    role_menus.c.tenant_id == tenant.tenant_id,
                    role_menus.c.role_id == super_role.role_id,
                    Menu.tenant_id == tenant.tenant_id,
                )
            )
        ).scalars()
    )
    assert assigned_permissions == HOSTED_PERMISSION_CODES
    enabled_published_agents = await db_session.scalar(
        select(func.count()).select_from(AiAgent).where(AiAgent.enabled.is_(True))
    )
    assert result.agent_binding_count <= (enabled_published_agents or 0)
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(RoleAiAgent)
            .where(RoleAiAgent.tenant_id == tenant.tenant_id)
        )
        == result.agent_binding_count
    )


async def test_bootstrap_replay_is_idempotent_and_changed_payload_conflicts(db_session):
    tenant = await _prepared_tenant(db_session)
    model = await _text_model(db_session)
    kwargs = {
        "tenant_id": tenant.tenant_id,
        "default_model_id": model.model_id,
        "admin_password": "TenantAdmin123",
        "idempotency_key": "tenant-bootstrap-idempotency-0002",
        "platform": _platform(PLATFORM_TENANT_BOOTSTRAP, tenant.tenant_id),
    }
    first = await tenant_bootstrap_service.bootstrap(db_session, **kwargs)
    replay = await tenant_bootstrap_service.bootstrap(db_session, **kwargs)

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.menu_count == first.menu_count
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.tenant_id == tenant.tenant_id)
        )
        == 1
    )

    with pytest.raises(BusinessException) as exc_info:
        await tenant_bootstrap_service.bootstrap(
            db_session,
            **(kwargs | {"admin_password": "DifferentPass123"}),
        )
    assert exc_info.value.code == 409
    assert exc_info.value.error_code == "PLATFORM_TENANT_BOOTSTRAP_IDEMPOTENCY_CONFLICT"


async def test_bootstrap_new_key_cannot_reconfigure_completed_tenant(db_session):
    tenant = await _prepared_tenant(db_session)
    model = await _text_model(db_session)
    platform = _platform(PLATFORM_TENANT_BOOTSTRAP, tenant.tenant_id)
    await tenant_bootstrap_service.bootstrap(
        db_session,
        tenant_id=tenant.tenant_id,
        default_model_id=model.model_id,
        admin_password="TenantAdmin123",
        idempotency_key="tenant-bootstrap-idempotency-0003",
        platform=platform,
    )

    with pytest.raises(BusinessException) as exc_info:
        await tenant_bootstrap_service.bootstrap(
            db_session,
            tenant_id=tenant.tenant_id,
            default_model_id=model.model_id,
            admin_password="TenantAdmin123",
            idempotency_key="tenant-bootstrap-idempotency-0004",
            platform=platform,
        )
    assert exc_info.value.error_code == "PLATFORM_TENANT_ALREADY_BOOTSTRAPPED"


async def test_bootstrap_key_cannot_cross_targets_and_other_tenant_has_no_side_effects(
    db_session,
):
    tenant_a = await _prepared_tenant(db_session)
    tenant_b = await _prepared_tenant(db_session)
    model = await _text_model(db_session)
    shared_key = "tenant-bootstrap-cross-target-key"
    await tenant_bootstrap_service.bootstrap(
        db_session,
        tenant_id=tenant_a.tenant_id,
        default_model_id=model.model_id,
        admin_password="TenantAdmin123",
        idempotency_key=shared_key,
        platform=_platform(PLATFORM_TENANT_BOOTSTRAP, tenant_a.tenant_id),
    )

    with pytest.raises(AuthorizationException) as exc_info:
        await tenant_bootstrap_service.bootstrap(
            db_session,
            tenant_id=tenant_b.tenant_id,
            default_model_id=model.model_id,
            admin_password="TenantAdmin123",
            idempotency_key=shared_key,
            platform=_platform(PLATFORM_TENANT_BOOTSTRAP, tenant_b.tenant_id),
        )
    assert exc_info.value.error_code == "PLATFORM_TARGET_TENANT_MISMATCH"
    await db_session.refresh(tenant_b)
    assert tenant_b.bootstrap_version == 0
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.tenant_id == tenant_b.tenant_id)
        )
        == 0
    )


async def test_bootstrap_failure_rolls_back_every_seed_and_marker(
    db_session, monkeypatch
):
    tenant = await _prepared_tenant(db_session)
    model = await _text_model(db_session)
    monkeypatch.setattr(
        ai_tenant_bootstrap_service,
        "seed",
        AsyncMock(side_effect=RuntimeError("simulated AI seed failure")),
    )

    with pytest.raises(RuntimeError, match="simulated AI seed failure"):
        await tenant_bootstrap_service.bootstrap(
            db_session,
            tenant_id=tenant.tenant_id,
            default_model_id=model.model_id,
            admin_password="TenantAdmin123",
            idempotency_key="tenant-bootstrap-idempotency-rollback",
            platform=_platform(PLATFORM_TENANT_BOOTSTRAP, tenant.tenant_id),
        )

    await db_session.refresh(tenant)
    assert tenant.bootstrap_version == 0
    assert tenant.bootstrap_key_hash is None
    assert tenant.bootstrap_fingerprint is None
    for model_type in (Menu, Role, User):
        assert (
            await db_session.scalar(
                select(func.count())
                .select_from(model_type)
                .where(model_type.tenant_id == tenant.tenant_id)
            )
            == 0
        )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(TenantAiModelPolicy)
            .where(TenantAiModelPolicy.tenant_id == tenant.tenant_id)
        )
        == 0
    )


async def test_bootstrap_rejects_write_only_permission_before_database_access():
    db = AsyncMock()
    with pytest.raises(AuthorizationException) as exc_info:
        await tenant_bootstrap_service.bootstrap(
            db,
            tenant_id=7001,
            default_model_id=8001,
            admin_password="TenantAdmin123",
            idempotency_key="tenant-bootstrap-idempotency-0005",
            platform=_platform(PLATFORM_TENANT_WRITE, 7001),
        )
    assert exc_info.value.error_code == "PLATFORM_PERMISSION_DENIED"
    db.execute.assert_not_awaited()
    db.scalar.assert_not_awaited()


async def test_bootstrap_service_rejects_weak_password_before_database_access():
    db = AsyncMock()
    with pytest.raises(BusinessException) as exc_info:
        await tenant_bootstrap_service.bootstrap(
            db,
            tenant_id=7001,
            default_model_id=8001,
            admin_password="weak",
            idempotency_key="tenant-bootstrap-idempotency-0006",
            platform=_platform(PLATFORM_TENANT_BOOTSTRAP, 7001),
        )
    assert exc_info.value.error_code == "INVALID_PASSWORD_FORMAT"
    db.execute.assert_not_awaited()
    db.scalar.assert_not_awaited()


def test_bootstrap_request_is_strict_and_keeps_password_write_only():
    payload = PlatformTenantBootstrapRequest.model_validate(
        {"defaultModelId": "8001", "adminPassword": "TenantAdmin123"}
    )
    assert payload.default_model_id == "8001"
    assert "TenantAdmin123" not in repr(payload)
    with pytest.raises(ValueError):
        PlatformTenantBootstrapRequest.model_validate(
            {
                "tenantId": "7001",
                "defaultModelId": "8001",
                "adminPassword": "TenantAdmin123",
            }
        )
    with pytest.raises(ValueError):
        PlatformTenantBootstrapRequest.model_validate(
            {"defaultModelId": "8001", "adminPassword": "weak"}
        )
    with pytest.raises(ValueError):
        PlatformTenantBootstrapRequest.model_validate(
            {"defaultModelId": 8001, "adminPassword": "TenantAdmin123"}
        )


async def test_concurrent_same_key_bootstrap_converges_to_one_seed():
    tenant_id = next_id()
    provider_id: int | None = None
    model_id: int | None = None
    key = f"tenant-bootstrap-concurrent-{tenant_id}"
    try:
        async with AsyncSessionLocal() as setup:
            tenant = Tenant(
                tenant_id=tenant_id,
                tenant_code=f"concurrent-{tenant_id}",
                tenant_name="Concurrent Bootstrap Tenant",
                status="2",
                lifecycle_state="prepared",
                bootstrap_version=0,
                row_version=1,
            )
            setup.add(tenant)
            await setup.flush()
            model = await _text_model(setup)
            provider_id = model.provider_id
            model_id = model.model_id
            await setup.commit()

        async def run_once():
            async with AsyncSessionLocal() as session:
                result = await tenant_bootstrap_service.bootstrap(
                    session,
                    tenant_id=tenant_id,
                    default_model_id=model_id,
                    admin_password="TenantAdmin123",
                    idempotency_key=key,
                    platform=_platform(PLATFORM_TENANT_BOOTSTRAP, tenant_id),
                )
                await session.commit()
                return result

        first, second = await asyncio.gather(run_once(), run_once())
        assert {first.replayed, second.replayed} == {False, True}
        async with AsyncSessionLocal() as verify:
            assert (
                await verify.scalar(
                    select(func.count())
                    .select_from(User)
                    .where(User.tenant_id == tenant_id)
                )
                == 1
            )
            assert (
                await verify.scalar(
                    select(func.count())
                    .select_from(TenantAiModelPolicy)
                    .where(TenantAiModelPolicy.tenant_id == tenant_id)
                )
                == 1
            )
    finally:
        async with AsyncSessionLocal() as cleanup:
            await cleanup.execute(
                delete(RoleAiAgent).where(RoleAiAgent.tenant_id == tenant_id)
            )
            await cleanup.execute(
                delete(TenantAiModelPolicy).where(
                    TenantAiModelPolicy.tenant_id == tenant_id
                )
            )
            await cleanup.execute(
                delete(user_roles).where(user_roles.c.tenant_id == tenant_id)
            )
            await cleanup.execute(
                delete(role_menus).where(role_menus.c.tenant_id == tenant_id)
            )
            await cleanup.execute(delete(User).where(User.tenant_id == tenant_id))
            await cleanup.execute(delete(Role).where(Role.tenant_id == tenant_id))
            await cleanup.execute(delete(Menu).where(Menu.tenant_id == tenant_id))
            await cleanup.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            if model_id is not None:
                await cleanup.execute(
                    delete(AiModel).where(AiModel.model_id == model_id)
                )
            if provider_id is not None:
                await cleanup.execute(
                    delete(AiProvider).where(AiProvider.provider_id == provider_id)
                )
            await cleanup.commit()
        try:
            await engine.dispose()
        except RuntimeError as exc:
            if "Event loop is closed" not in str(exc):
                raise
