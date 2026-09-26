from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.exceptions import AuthorizationException, BusinessRuleException
from app.core.tenant import TenantContext
from app.modules.system.api.settings import _ensure_group_access
from app.modules.system.models.config import Config
from app.modules.system.models.setting import SystemSetting
from app.modules.system.schemas.config import ConfigCreate, ConfigUpdate
from app.modules.system.service.config_service import config_service
from app.modules.system.service.settings_service import settings_service
from app.modules.system.settings_catalog import UPLOAD_EXTENSION_UNIVERSE
from tests.tenant_helpers import create_test_tenant, tenant_context

TENANT = TenantContext(0, "default", 1, 1, "access_token")


async def test_settings_group_is_atomic_and_keeps_custom_parameters(db_session):
    initial = await settings_service.get_group(db_session, "brand", tenant=TENANT)
    await settings_service.save_group(
        db_session,
        "brand",
        {"site_name": "Our workspace"},
        revision=initial["revision"],
        tenant=TENANT,
    )
    assert (
        await settings_service.get_value(db_session, "site_name", tenant=TENANT)
        == "Our workspace"
    )
    with pytest.raises(BusinessRuleException):
        await settings_service.save_group(
            db_session,
            "brand",
            {"site_name": "Overwrite", "unknown": True},
            revision=initial["revision"],
            tenant=TENANT,
        )
    assert (
        await db_session.scalar(
            select(SystemSetting.setting_value).where(
                SystemSetting.tenant_id == 0, SystemSetting.setting_key == "site_name"
            )
        )
        == "Our workspace"
    )


async def test_stale_settings_write_is_rejected(db_session):
    initial = await settings_service.get_group(db_session, "brand", tenant=TENANT)
    await settings_service.save_group(
        db_session,
        "brand",
        {"site_name": "First"},
        revision=initial["revision"],
        tenant=TENANT,
    )
    with pytest.raises(BusinessRuleException, match="changed"):
        await settings_service.save_group(
            db_session,
            "brand",
            {"site_name": "Second"},
            revision=initial["revision"],
            tenant=TENANT,
        )


async def test_builtin_key_cannot_be_created_via_custom_parameters(db_session):
    with pytest.raises(BusinessRuleException):
        await config_service.create(
            db_session,
            ConfigCreate(
                config_name="Public password",
                config_key="auth:default_password",
                config_value="secret",
                config_group="custom",
                status="1",
                is_public=True,
            ),
            tenant=TENANT,
        )


async def test_account_settings_do_not_return_password(db_session):
    current = await settings_service.get_group(db_session, "account", tenant=TENANT)
    assert current["values"]["auth:default_password"] == ""
    assert "auth:default_password" in current["secret_keys"]


async def test_upload_limits_must_fit_deployment_cap(db_session):
    current = await settings_service.get_group(db_session, "files", tenant=TENANT)
    with pytest.raises(BusinessRuleException):
        await settings_service.save_group(
            db_session,
            "files",
            {"upload:max_bytes": 2**50},
            revision=current["revision"],
            tenant=TENANT,
        )


async def test_preferences_are_scoped_and_secret_is_never_inherited(db_session):
    other = await create_test_tenant(db_session, prefix="settings")
    tenant = tenant_context(tenant_id=other.tenant_id, actor_user_id=1)
    current = await settings_service.get_group(db_session, "account", tenant=tenant)
    assert "auth:default_password" not in current["configured_secrets"]
    current = await settings_service.get_group(db_session, "brand", tenant=tenant)
    await settings_service.save_group(
        db_session,
        "brand",
        {"site_name": "Tenant brand"},
        revision=current["revision"],
        tenant=tenant,
    )
    assert (await settings_service.values(db_session, tenant_id=other.tenant_id))[
        "site_name"
    ] == "Tenant brand"
    assert (await settings_service.values(db_session, tenant_id=TENANT.tenant_id))[
        "site_name"
    ] != "Tenant brand"


async def test_generic_update_and_delete_cannot_change_builtin_definition(db_session):
    # Even a legacy/direct SQL row cannot bypass reserved-key protection.
    row = Config(
        tenant_id=0,
        config_key="site_name",
        config_name="Legacy brand",
        config_value="Legacy",
        config_group="basic",
        status="1",
    )
    db_session.add(row)
    await db_session.flush()
    for operation in [
        config_service.update(
            db_session,
            row.config_id,
            ConfigUpdate(config_key="custom:renamed"),
            tenant=TENANT,
        ),
        config_service.delete(db_session, row.config_id, tenant=TENANT),
        config_service.batch_delete(db_session, [row.config_id], tenant=TENANT),
    ]:
        with pytest.raises(BusinessRuleException):
            await operation
    assert row.config_key == "site_name"


@pytest.mark.parametrize(
    "values",
    [
        {"register_enabled": "false"},
        {"default_avatar": "javascript:alert(1)"},
        {"auth:default_password": "123"},
    ],
)
async def test_account_fields_are_validated_as_a_group(db_session, values):
    current = await settings_service.get_group(db_session, "account", tenant=TENANT)
    with pytest.raises(BusinessRuleException):
        await settings_service.save_group(
            db_session, "account", values, revision=current["revision"], tenant=TENANT
        )


def test_tenant_admin_cannot_write_global_security_settings():
    user = SimpleNamespace(roles=[SimpleNamespace(role_code="R_SUPER", status="1")])
    with pytest.raises(AuthorizationException):
        _ensure_group_access(
            "security", user, tenant_context(tenant_id=42, actor_user_id=1)
        )


async def test_upload_extension_field_serves_option_universe(db_session):
    current = await settings_service.get_group(db_session, "files", tenant=TENANT)
    field = next(
        item for item in current["fields"] if item["key"] == "upload:allowed_extensions"
    )
    assert field["kind"] == "extensions"
    assert tuple(field["options"]) == tuple(UPLOAD_EXTENSION_UNIVERSE)
    assert {".md", ".json", ".pptx", ".7z", ".mp4", ".ico"} <= set(
        UPLOAD_EXTENSION_UNIVERSE
    )
    # 存量租户已保存的白名单不被全集扩充改写；新租户回退到目录默认值。
    other = await create_test_tenant(db_session, prefix="universe")
    fresh = await settings_service.get_group(
        db_session,
        "files",
        tenant=tenant_context(tenant_id=other.tenant_id, actor_user_id=1),
    )
    assert set(str(fresh["values"]["upload:allowed_extensions"]).split(",")) == set(
        UPLOAD_EXTENSION_UNIVERSE
    )


async def test_upload_extensions_save_multiple_choices_and_preserve_invalid_write(
    db_session,
):
    current = await settings_service.get_group(db_session, "files", tenant=TENANT)
    values = {**current["values"], "upload:allowed_extensions": ".png, .md,.json,.png"}
    saved = await settings_service.save_group(
        db_session, "files", values, revision=current["revision"], tenant=TENANT
    )
    assert saved["values"]["upload:allowed_extensions"] == ".json,.md,.png"
    for invalid in (".png,.exe", ".png,.html", "", ".png,"):
        with pytest.raises(BusinessRuleException):
            await settings_service.save_group(
                db_session,
                "files",
                {"upload:allowed_extensions": invalid},
                revision=saved["revision"],
                tenant=TENANT,
            )
    after = await settings_service.get_group(db_session, "files", tenant=TENANT)
    assert after["values"] == saved["values"]
    assert after["revision"] == saved["revision"]
