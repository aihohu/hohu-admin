"""Real PostgreSQL proofs for dormant System configuration candidates."""

from io import BytesIO

import pytest
from openpyxl import Workbook
from sqlalchemy import insert, select, update

from app.core.exceptions import BusinessException
from app.db.authorization_control_owner import control_candidate_owner
from app.db.base import role_menus
from app.modules.system.models.config import Config
from app.modules.system.models.mail_config import MailConfig
from app.modules.system.models.menu import Menu
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard,
    UserFieldPolicyState,
)
from app.modules.system.service import mail_service as mail_transport
from app.modules.system.service.authorization_configuration_candidate import (
    configuration_authorization_candidate as candidate,
)
from app.modules.system.service.settings_service import settings_service
from tests.modules.system.test_authorization_system_candidate import (
    TENANT,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


@pytest.fixture
async def configuration_sessions(system_sessions):
    factory, plain = system_sessions
    async with plain() as db:
        await db.execute(insert(MailConfig).values(config_id=1, values={}, version=1))
        await db.execute(
            update(UserFieldPolicyState).values(
                epoch=2, mode="ready", candidate_manifest_digest="a" * 64
            )
        )
        await db.commit()
    return factory, plain


async def execute(db, operation, payload=None, **kwargs):
    return (
        await candidate.execute(
            db,
            tenant=TENANT,
            actor_user_version=1,
            operation=operation,
            payload=payload,
            **kwargs,
        )
    ).value


async def intent(factory, operation):
    async with factory() as db:
        return await control_candidate_owner.authorize(
            db,
            tenant=TENANT,
            actor_user_version=1,
            operation=operation,
            permission="system:setting:edit",
            reason="test configuration",
            ticket_id="CONFIG-1",
            correlation_id="config-correlation",
            target_tenant_id=None,
            path="/test/configuration",
            method="POST",
        )


async def epochs(plain):
    async with plain() as db:
        g = await db.scalar(select(UserFieldPolicyGlobalGuard.epoch))
        states = (
            await db.execute(
                select(
                    UserFieldPolicyState.tenant_id,
                    UserFieldPolicyState.epoch,
                    UserFieldPolicyState.mode,
                ).order_by(UserFieldPolicyState.tenant_id)
            )
        ).all()
        return g, [tuple(row) for row in states]


async def test_mail_send_requires_closed_read_and_is_single_use(
    configuration_sessions, monkeypatch
):
    factory, plain = configuration_sessions
    async with plain() as db:
        await db.execute(
            update(MailConfig).values(
                values={
                    "host": "smtp.example.com",
                    "port": 587,
                    "from_email": "sender@example.com",
                    "enabled": True,
                }
            )
        )
        await db.commit()
    audit = await intent(factory, "mail.test")
    sent = []
    monkeypatch.setattr(mail_transport, "_send", lambda *_args: sent.append(True))
    before = await epochs(plain)
    async with factory() as db:
        prepared = await candidate.prepare_mail_test(
            db,
            tenant=TENANT,
            actor_user_version=1,
            payload={"recipient": "recipient@example.com"},
            audit=audit,
        )
        with pytest.raises(BusinessException):
            await candidate.run_mail_test(prepared)
        assert not sent
        await db.rollback()
        outcome = await candidate.run_mail_test(prepared)
        assert outcome.accepted and len(sent) == 1
        with pytest.raises(BusinessException):
            await candidate.run_mail_test(prepared)
        result = await candidate.complete_mail_test(
            db,
            tenant=TENANT,
            actor_user_version=1,
            preparation=prepared,
            outcome=outcome,
            audit=audit,
        )
        await db.commit()
        assert result["lastTestStatus"] == "accepted"
        with pytest.raises(BusinessException):
            await candidate.complete_mail_test(
                db,
                tenant=TENANT,
                actor_user_version=1,
                preparation=prepared,
                outcome=outcome,
                audit=audit,
            )
    assert await epochs(plain) == before


async def test_mail_late_config_change_rejects_stale_completion(
    configuration_sessions, monkeypatch
):
    factory, plain = configuration_sessions
    async with plain() as db:
        await db.execute(
            update(MailConfig).values(
                values={
                    "host": "smtp.example.com",
                    "port": 587,
                    "from_email": "sender@example.com",
                    "enabled": True,
                }
            )
        )
        await db.commit()
    audit = await intent(factory, "mail.test")
    monkeypatch.setattr(mail_transport, "_send", lambda *_args: None)
    async with factory() as db:
        prepared = await candidate.prepare_mail_test(
            db,
            tenant=TENANT,
            actor_user_version=1,
            payload="recipient@example.com",
            audit=audit,
        )
        await db.rollback()
        outcome = await candidate.run_mail_test(prepared)
        async with plain() as other:
            await other.execute(update(MailConfig).values(version=2))
            await other.commit()
        with pytest.raises(BusinessException) as error:
            await candidate.complete_mail_test(
                db,
                tenant=TENANT,
                actor_user_version=1,
                preparation=prepared,
                outcome=outcome,
                audit=audit,
            )
        assert error.value.error_code == "SETTINGS_CONFLICT"
        await db.rollback()
    async with plain() as db:
        assert await db.scalar(select(MailConfig.last_test_at)) is None


def config_payload(key="custom.example", value="value"):
    return {
        "config_name": "Example",
        "config_key": key,
        "config_value": value,
        "config_group": "custom",
        "status": "1",
    }


def workbook(*rows):
    book = Workbook()
    book.active.append(["name", "key", "value", "type", "group", "status", "public"])
    for row in rows:
        book.active.append(row)
    out = BytesIO()
    book.save(out)
    book.close()
    return out.getvalue()


async def test_menu_root_query_and_buttons_are_one_actual_epoch(configuration_sessions):
    factory, plain = configuration_sessions
    async with factory() as db:
        result = await execute(
            db,
            "menu.create",
            {
                "menu_name": "Example",
                "menu_type": "C",
                "status": "1",
                "parent_id": 0,
                "query": [{"key": "ignored"}],
                "buttons": [{"desc": "Read", "code": "custom:read"}],
            },
        )
        target = int(result["menu_id"])
        await db.commit()
    async with plain() as db:
        row = await db.scalar(select(Menu).where(Menu.menu_id == target))
        button = await db.scalar(select(Menu).where(Menu.parent_id == target))
        assert row.parent_id is None and row.query is None
        button_id = button.menu_id
        await db.execute(
            insert(role_menus).values(tenant_id=0, role_id=201, menu_id=button_id)
        )
        await db.commit()
    async with factory() as db:
        await execute(
            db,
            "menu.update",
            {
                "menu_name": "Example",
                "menu_type": "C",
                "status": "1",
                "query": [{"key": "kept"}],
                "buttons": [{"desc": "Read renamed", "code": "custom:read"}],
            },
            target_id=target,
        )
        await db.commit()
    async with plain() as db:
        row = await db.scalar(select(Menu).where(Menu.menu_id == target))
        assert row.query == [{"key": "kept"}]
        assert (
            await db.scalar(select(Menu.menu_id).where(Menu.parent_id == target))
            == button_id
        )
        assert (
            await db.scalar(
                select(role_menus.c.menu_id).where(role_menus.c.menu_id == button_id)
            )
            == button_id
        )
    assert await epochs(plain) == (1, [(0, 4, "legacy"), (1, 2, "ready")])


async def test_menu_explicit_junction_delete_and_child_before_parent(
    configuration_sessions,
):
    factory, plain = configuration_sessions
    async with plain() as db:
        await db.execute(
            insert(Menu).values(
                tenant_id=0, menu_id=3001, menu_name="Parent", menu_type="C", status="1"
            )
        )
        await db.execute(
            insert(Menu).values(
                tenant_id=0,
                menu_id=3002,
                parent_id=3001,
                menu_name="Button",
                menu_type="F",
                status="1",
            )
        )
        await db.execute(
            insert(role_menus).values(tenant_id=0, role_id=201, menu_id=3002)
        )
        await db.commit()
    async with factory() as db:
        with pytest.raises(BusinessException):
            await execute(db, "menu.delete", target_id=3001)
        await db.rollback()
    async with factory() as db:
        assert await execute(db, "menu.batch_delete", [3001, 3002, 3002]) == 2
        await db.commit()
    async with plain() as db:
        assert await db.scalar(select(Menu.menu_id).where(Menu.menu_id == 3001)) is None
        assert (
            await db.scalar(
                select(role_menus.c.menu_id).where(role_menus.c.menu_id == 3002)
            )
            is None
        )
    assert await epochs(plain) == (1, [(0, 3, "legacy"), (1, 2, "ready")])


async def test_config_mask_noop_builtin_and_cross_tenant(configuration_sessions):
    factory, plain = configuration_sessions
    async with factory() as db:
        result = await execute(
            db, "config.create", config_payload("custom.secret", "private-value")
        )
        target = int(result["config_id"])
        assert "private-value" not in repr(result)
        await db.commit()
    async with factory() as db:
        await execute(db, "config.update", {"config_value": "******"}, target_id=target)
        await db.commit()
    async with factory() as db:
        with pytest.raises(BusinessException) as error:
            await execute(db, "config.create", config_payload("ai:enabled_tools"))
        assert error.value.error_code == "BUILTIN_SETTING_PROTECTED"
        await db.rollback()
    async with factory() as db:
        with pytest.raises(BusinessException):
            await execute(db, "config.delete", target_id=target, target_tenant_id=1)
        await db.rollback()
    assert await epochs(plain) == (1, [(0, 3, "legacy"), (1, 2, "ready")])


async def test_import_preserves_skip_and_string_bool_without_overwrite(
    configuration_sessions,
):
    factory, plain = configuration_sessions
    prepared = await candidate.parse_import(
        workbook(
            ["A", "custom.first", "one", "text", "g", "1", "false"],
            ["B", "custom.first", "two", "text", "g", "1", False],
            ["Builtin", "security:login_per_minute", "bad", "text", "g", "1", False],
        )
    )
    async with factory() as db:
        assert await execute(db, "config.import", prepared) == {
            "success": 1,
            "skipped": 2,
        }
        await db.commit()
    async with plain() as db:
        row = await db.scalar(select(Config).where(Config.config_key == "custom.first"))
        assert row.config_value == "one" and row.is_public is True
    async with factory() as db:
        assert await execute(db, "config.import", prepared) == {
            "success": 0,
            "skipped": 3,
        }
        await db.commit()
    assert await epochs(plain) == (1, [(0, 3, "legacy"), (1, 2, "ready")])


@pytest.mark.parametrize(
    "operation,payload",
    [
        (
            "menu.create",
            {"menu_name": "X", "menu_type": "C", "status": "1", "icon_type": "xx"},
        ),
        (
            "menu.create",
            {
                "menu_name": "X",
                "menu_type": "C",
                "status": "1",
                "buttons": [{"desc": "x", "code": "x"}] * 129,
            },
        ),
        ("menu.batch_delete", [1] * 257),
        ("config.create", config_payload(value="x" * 8193)),
        (
            "settings.save",
            {"group": "ai", "values": {"ai:enabled_tools": "[]"}, "revision": "old"},
        ),
    ],
)
async def test_preflight_limits_leave_no_transaction(
    configuration_sessions, operation, payload
):
    factory, _ = configuration_sessions
    async with factory() as db:
        with pytest.raises(BusinessException):
            await execute(db, operation, payload)
        assert not db.in_transaction()


async def test_settings_revision_noop_and_global_security(configuration_sessions):
    factory, plain = configuration_sessions
    async with plain() as db:
        current = await settings_service.get_group(db, "brand", tenant=TENANT)
    async with factory() as db:
        result = await execute(
            db,
            "settings.save",
            {
                "group": "brand",
                "values": {"site_name": "Candidate"},
                "revision": current["revision"],
            },
        )
        await db.commit()
    async with factory() as db:
        await execute(
            db,
            "settings.save",
            {
                "group": "brand",
                "values": {"site_name": "Candidate"},
                "revision": result["revision"],
            },
        )
        await db.commit()
    async with plain() as db:
        security = await settings_service.get_group(db, "security", tenant=TENANT)
    audit = await intent(factory, "settings.save")
    async with factory() as db:
        await execute(
            db,
            "settings.save",
            {
                "group": "security",
                "values": {"security:login_per_minute": 19},
                "revision": security["revision"],
            },
            audit=audit,
        )
        await db.commit()
    assert await epochs(plain) == (2, [(0, 4, "legacy"), (1, 3, "legacy")])


async def test_mail_global_change_then_plaintext_noop_only_versions(
    configuration_sessions,
):
    factory, plain = configuration_sessions
    payload = {
        "values": {
            "host": "smtp.example.com",
            "from_email": "sender@example.com",
            "username": "sender",
            "password": "private-mail-password",
            "enabled": True,
        },
        "revision": "1",
    }
    audit = await intent(factory, "mail.save")
    async with factory() as db:
        result = await execute(db, "mail.save", payload, audit=audit)
        assert result["revision"] == "2" and result["values"]["password"] == ""
        await db.commit()
    async with plain() as db:
        encrypted = await db.scalar(select(MailConfig.password_ciphertext))
    audit = await intent(factory, "mail.save")
    payload["revision"] = "2"
    payload["values"]["password"] = ""
    async with factory() as db:
        result = await execute(db, "mail.save", payload, audit=audit)
        assert result["revision"] == "3"
        await db.commit()
    async with plain() as db:
        assert await db.scalar(select(MailConfig.password_ciphertext)) == encrypted
    assert await epochs(plain) == (2, [(0, 3, "legacy"), (1, 3, "legacy")])


async def test_global_audit_is_required_before_any_sql(configuration_sessions):
    factory, _ = configuration_sessions
    async with factory() as db:
        with pytest.raises(BusinessException):
            await execute(db, "mail.save", {"values": {}, "revision": "1"})
        assert not db.in_transaction()
