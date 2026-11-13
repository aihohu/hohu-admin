"""Publication has an explicit owner, current proof and fresh public reads."""

import pytest
from sqlalchemy import delete, insert, select, update

from app.core.exceptions import BusinessException
from app.core.tenant import TenantLocatorContext
from app.db.authorization_parameter_read_owner import parameter_read_owner
from app.db.base import role_menus
from app.modules.system.field_policy.control_catalog import control_catalog
from app.modules.system.hosted_menu_seed import HOSTED_BUTTON_PERMISSIONS
from app.modules.system.menu_seed import MENU_DEFINITIONS
from app.modules.system.models.config import Config
from app.modules.system.models.config_publication import ConfigPublication
from app.modules.system.models.menu import Menu
from app.modules.system.schemas.parameter_form import ParameterFormQuery
from app.modules.system.service import authorization_read_budget as read_budget
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_parameter_config_writes import write
from tests.modules.system.test_parameter_read_owner import parameters as parameters
from tests.modules.system.test_parameter_read_owner import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_parameter_read_owner import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_relation_field_writes import activate_relations


def test_publication_has_its_own_installation_and_hosted_button():
    definitions = [
        row
        for row in MENU_DEFINITIONS
        if row.get("permission") == "system:config:publish"
    ]
    assert len(definitions) == 1 and definitions[0]["menu_type"] == "F"
    assert "system:config:publish" in HOSTED_BUTTON_PERMISSIONS


async def form(factory, target=301):
    async with factory() as db:
        return await parameter_read_owner.execute(
            db,
            tenant=TENANT,
            actor_user_version=1,
            operation="config.form",
            query=ParameterFormQuery(mode="update", record_id=str(target)),
        )


async def public(factory):
    async with factory() as db:
        return await parameter_read_owner.execute(
            db,
            tenant=TenantLocatorContext(0, "plan-0", 1),
            actor_user_version=None,
            operation="config.public",
        )


async def grant_publish(plain):
    async with plain() as db:
        await db.execute(
            insert(Menu).values(
                tenant_id=0,
                menu_id=9901,
                menu_name="Publish",
                menu_type="F",
                permission="system:config:publish",
                status="1",
            )
        )
        await db.execute(
            insert(role_menus).values(tenant_id=0, role_id=201, menu_id=9901)
        )
        await db.commit()


async def test_explicit_publish_permission_cannot_be_inferred_from_super_role(
    parameters,
):
    await grant_publish(parameters[1])
    current = await form(parameters[0])
    async with parameters[1]() as db:
        await db.execute(delete(role_menus).where(role_menus.c.menu_id == 9901))
        await db.commit()
    with pytest.raises(BusinessException) as denied:
        await write(
            parameters[0],
            "config.publish",
            {"confirmed": True, "expected_revision": current["publicationRevision"]},
            301,
        )
    assert denied.value.error_code == "MISSING_PERMISSION"
    assert await public(parameters[0]) == {}


async def test_legacy_public_flag_is_not_a_publication(parameters):
    async with parameters[1]() as db:
        await db.execute(update(Config).values(is_public=True))
        await db.commit()
    assert await public(parameters[0]) == {}


async def test_form_does_not_fetch_or_issue_publish_proof_for_hidden_value(
    parameters, monkeypatch
):
    await grant_publish(parameters[1])
    await activate_relations(
        parameters,
        monkeypatch,
        catalog=control_catalog(),
        missing=(("system.config", "config_value", "read"),),
    )
    original = read_budget._size

    def observe(value):
        assert "value-301" not in repr(value)
        return original(value)

    monkeypatch.setattr(read_budget, "_size", observe)
    current = await form(parameters[0])
    assert "publicationRevision" not in current


async def test_revocation_needs_no_hidden_content_read(parameters, monkeypatch):
    factory, plain = parameters
    await grant_publish(plain)
    current = await form(factory)
    await write(
        factory,
        "config.publish",
        {"confirmed": True, "expected_revision": current["publicationRevision"]},
        301,
    )
    await activate_relations(
        parameters,
        monkeypatch,
        catalog=control_catalog(),
        missing=(("system.config", "config_value", "read"),),
    )
    current = await form(factory)
    assert "publicationRevision" not in current
    assert current.get("revocationRevision")
    assert await write(
        factory,
        "config.revoke",
        {"expected_revision": current["revocationRevision"]},
        301,
    ) == {"published": False}
    assert await public(factory) == {}


async def test_publication_binding_invalidates_on_change_without_cache(parameters):
    factory, plain = parameters
    await grant_publish(plain)
    current = await form(factory)
    assert current["record"]["configValue"] == "value-301" and current["formReceipt"]
    payload = {"confirmed": True, "expected_revision": current["publicationRevision"]}
    assert await write(factory, "config.publish", payload, 301) == {"published": True}
    assert await public(factory) == {"business.301": "value-301"}
    with pytest.raises(BusinessException) as stale:
        await write(factory, "config.publish", payload, 301)
    assert stale.value.code == 409
    await write(factory, "config.update", {"config_value": "new value"}, 301)
    assert await public(factory) == {}
    await write(factory, "config.update", {"config_value": "value-301"}, 301)
    assert await public(factory) == {}
    await write(factory, "config.update", {"config_value": "new value"}, 301)
    current = await form(factory)
    await write(
        factory,
        "config.publish",
        {"confirmed": True, "expected_revision": current["publicationRevision"]},
        301,
    )
    assert await public(factory) == {"business.301": "new value"}
    current = await form(factory)
    await write(
        factory,
        "config.revoke",
        {"expected_revision": current["revocationRevision"]},
        301,
    )
    assert await public(factory) == {}
    async with plain() as db:
        assert list(await db.scalars(select(ConfigPublication.config_id))) == []


@pytest.mark.parametrize("target", [302, 303])
async def test_unknown_or_secret_content_cannot_be_published(parameters, target):
    await grant_publish(parameters[1])
    current = await form(parameters[0], target)
    assert "configValue" not in current["record"]
    assert "publicationRevision" not in current
    with pytest.raises(BusinessException):
        await write(
            parameters[0],
            "config.publish",
            {"confirmed": True, "expected_revision": "a" * 43},
            target,
        )
    assert await public(parameters[0]) == {}
