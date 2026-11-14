"""Setting values, revisions and mail use current owners without secret fetches."""

import pytest
from sqlalchemy import insert, update

from app.core.exceptions import BusinessException
from app.db.authorization_parameter_read_owner import parameter_read_owner
from app.db.base import role_menus
from app.modules.system.field_policy.ordinary_catalog import (
    production_catalog as control_catalog,
)
from app.modules.system.models.mail_config import MailConfig
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.setting import SystemSetting
from app.modules.system.service import authorization_read_budget as budget_module
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_parameter_config_writes import write
from tests.modules.system.test_parameter_read_owner import (
    parameters as parameters,
)
from tests.modules.system.test_parameter_read_owner import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_parameter_read_owner import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_relation_field_writes import activate_relations


async def group(factory, name="brand"):
    async with factory() as db:
        return await parameter_read_owner.execute(
            db,
            tenant=TENANT,
            actor_user_version=1,
            operation="settings.group",
            query=name,
        )


async def test_hidden_values_and_secret_metadata_are_filtered_but_revision_is_complete(
    parameters, monkeypatch
):
    factory, plain = parameters
    async with plain() as db:
        await db.execute(
            insert(SystemSetting),
            [
                {
                    "tenant_id": 0,
                    "setting_key": "site_name",
                    "setting_value": "Visible",
                    "status": "1",
                },
                {
                    "tenant_id": 0,
                    "setting_key": "site_description",
                    "setting_value": "Hidden original",
                    "status": "1",
                },
                {
                    "tenant_id": 0,
                    "setting_key": "auth:default_password",
                    "setting_value": "Never leave SQL",
                    "status": "1",
                },
            ],
        )
        await db.commit()
    await activate_relations(
        parameters,
        monkeypatch,
        catalog=control_catalog(),
        missing=(
            ("system.setting", "site_description", "read"),
            ("system.setting", "auth:default_password", "update"),
        ),
    )
    before = await group(factory)
    assert (
        before["values"]["site_name"] == "Visible"
        and "site_description" not in before["values"]
    )
    account = await group(factory, "account")
    assert "auth:default_password" not in account["values"]
    assert account["configuredSecrets"] == [] and account["secretKeys"] == []
    assert all(field["key"] != "auth:default_password" for field in account["fields"])
    async with plain() as db:
        await db.execute(
            update(SystemSetting)
            .where(SystemSetting.setting_key == "site_description")
            .values(setting_value="Hidden replacement")
        )
        await db.commit()
    after = await group(factory)
    assert (
        before["values"] == after["values"] and before["revision"] != after["revision"]
    )
    assert "Hidden" not in repr(after) and "Never leave SQL" not in repr(after)


async def test_group_write_authorizes_actual_dirty_keys_not_only_visible_values(
    parameters, monkeypatch
):
    await activate_relations(
        parameters,
        monkeypatch,
        catalog=control_catalog(),
        missing=(
            ("system.setting", "site_description", "read"),
            ("system.setting", "site_logo", "update"),
        ),
    )
    current = await group(parameters[0])
    await write(
        parameters[0],
        "settings.save",
        {
            "group": "brand",
            "revision": current["revision"],
            "values": {"site_description": "blind update"},
        },
    )
    current = await group(parameters[0])
    with pytest.raises(BusinessException) as denied:
        await write(
            parameters[0],
            "settings.save",
            {
                "group": "brand",
                "revision": current["revision"],
                "values": {"site_logo": "https://example.com/logo.png"},
            },
        )
    assert denied.value.error_code == "FIELD_POLICY_DENIED"


async def test_mail_get_never_decrypts_password_or_returns_unknown_keys(
    parameters, monkeypatch
):
    factory, plain = parameters
    async with plain() as db:
        await db.execute(
            insert(MailConfig).values(
                config_id=1,
                values={
                    "enabled": True,
                    "host": "smtp.example.com",
                    "port": 587,
                    "security": "STARTTLS",
                    "username": "sender",
                    "from_email": "sender@example.com",
                    "unexpected": "hidden extra",
                },
                password_ciphertext="not-a-valid-cipher",
                version=2,
            )
        )
        await db.commit()
    observed = []
    original = budget_module._size

    def capture(value):
        if isinstance(value, str) and (
            "hidden extra" in value or "not-a-valid-cipher" in value
        ):
            observed.append(value)
        return original(value)

    monkeypatch.setattr(budget_module, "_size", capture)
    async with factory() as db:
        result = await parameter_read_owner.execute(
            db, tenant=TENANT, actor_user_version=1, operation="mail.describe"
        )
    assert (
        result["configuredPassword"]
        and result["ready"]
        and result["values"]["password"] == ""
    )
    assert "not-a-valid-cipher" not in repr(result) and "hidden extra" not in repr(
        result
    )
    assert observed == []


@pytest.mark.parametrize("hidden", [False, True])
async def test_registration_selector_requires_complete_real_role_sources(
    parameters, monkeypatch, hidden
):
    async with parameters[1]() as db:
        await db.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=701,
                role_code="R_PUBLIC",
                role_name="Public member",
                status="1",
                data_scope="5",
            )
        )
        await db.commit()
    await activate_relations(
        parameters,
        monkeypatch,
        catalog=control_catalog(),
        missing=(("system.role", "role_name", "read"),) if hidden else (),
    )
    result = await group(parameters[0], "account")
    assert result["roleOptionsComplete"] is (not hidden)
    if hidden:
        assert "roleOptions" not in result and "Public member" not in repr(result)
    else:
        assert result["roleOptions"] == [{"label": "Public member", "value": "701"}]


@pytest.mark.parametrize("hidden", [False, True])
async def test_registration_write_rechecks_the_current_role_source(
    parameters, monkeypatch, hidden
):
    async with parameters[1]() as db:
        await db.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=701,
                role_code="R_PUBLIC",
                role_name="Public member",
                status="1",
                data_scope="5",
            )
        )
        await db.commit()
    await activate_relations(
        parameters,
        monkeypatch,
        catalog=control_catalog(),
        missing=(("system.role", "role_name", "read"),) if hidden else (),
    )
    current = await group(parameters[0], "account")
    payload = {
        "group": "account",
        "revision": current["revision"],
        "values": {"registration_role_id": "701"},
    }
    if hidden:
        with pytest.raises(BusinessException) as denied:
            await write(parameters[0], "settings.save", payload)
        assert denied.value.error_code == "FIELD_POLICY_DENIED"
    else:
        result = await write(parameters[0], "settings.save", payload)
        assert result["values"]["registration_role_id"] == "701"


async def test_registration_role_cannot_delegate_unowned_business_permissions(
    parameters, monkeypatch
):
    async with parameters[1]() as db:
        await db.execute(
            update(Role).where(Role.role_id == 201).values(role_code="R_OPERATOR")
        )
        await db.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=702,
                role_code="R_BUSINESS",
                role_name="Business member",
                status="1",
                data_scope="5",
            )
        )
        for identifier, permission in enumerate(
            ("system:setting:list", "system:setting:edit", "system:user:role-auth"),
            9910,
        ):
            await db.execute(
                insert(Menu).values(
                    tenant_id=0,
                    menu_id=identifier,
                    menu_name="Permission",
                    menu_type="F",
                    permission=permission,
                    status="1",
                )
            )
            await db.execute(
                insert(role_menus).values(tenant_id=0, role_id=201, menu_id=identifier)
            )
        await db.execute(
            insert(Menu).values(
                tenant_id=0,
                menu_id=9920,
                menu_name="Business write",
                menu_type="F",
                permission="business:orders:delete",
                status="1",
            )
        )
        await db.execute(
            insert(role_menus).values(tenant_id=0, role_id=702, menu_id=9920)
        )
        await db.commit()
    await activate_relations(parameters, monkeypatch, catalog=control_catalog())
    current = await group(parameters[0], "account")
    assert current["roleOptionsComplete"] and current["roleOptions"] == []
    with pytest.raises(BusinessException) as denied:
        await write(
            parameters[0],
            "settings.save",
            {
                "group": "account",
                "revision": current["revision"],
                "values": {"registration_role_id": "702"},
            },
        )
    assert denied.value.error_code == "REGISTRATION_ROLE_INVALID"
