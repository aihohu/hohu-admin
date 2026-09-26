"""Execute release migrations in a disposable, transaction-isolated schema."""

from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema

from app.core.config import settings


def release_chain():
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    revisions = list(reversed(list(scripts.walk_revisions())))
    assert [item.revision for item in revisions] == [
        "bf244f9a8b76",
        "e7cc9aa08769",
        "8946c48f5315",
        "b728ae092401",
        "c926ae092601",
    ]
    return [item.module for item in revisions]


@pytest.fixture
async def migration_connection():
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            schema = "migration_review_" + uuid4().hex
            await connection.execute(CreateSchema(schema))
            await connection.execute(
                sa.select(sa.func.set_config("search_path", schema, True))
            )
            yield connection
        finally:
            await transaction.rollback()
    await engine.dispose()


def apply_steps(connection, modules, direction):
    with Operations.context(MigrationContext.configure(connection)):
        for module in modules:
            getattr(module, direction)()


async def test_fresh_install_preserves_security_triggers(migration_connection):
    connection = migration_connection
    await connection.run_sync(apply_steps, release_chain(), "upgrade")
    triggers = set(
        await connection.scalars(
            sa.text(
                "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=current_schema() AND NOT t.tgisinternal"
            )
        )
    )
    assert {
        "trg_platform_audit_append_only",
        "trg_platform_audit_validate_lineage",
        "trg_platform_principal_security_version",
        "trg_sys_tenant_security_version",
    } <= triggers
    await connection.execute(
        sa.text(
            "INSERT INTO sys_platform_principal "
            "(principal_id, principal_name, display_name, hashed_password) "
            "VALUES (1, 'migration-test', 'Migration test', 'not-a-password')"
        )
    )
    await connection.execute(
        sa.text("UPDATE sys_platform_principal SET status='2' WHERE principal_id=1")
    )
    assert (
        await connection.scalar(
            sa.text(
                "SELECT row_version FROM sys_platform_principal WHERE principal_id=1"
            )
        )
        == 2
    )


async def test_empty_release_upgrade_downgrade_roundtrip(migration_connection):
    connection = migration_connection
    chain = release_chain()
    await connection.run_sync(apply_steps, chain, "upgrade")
    await connection.run_sync(apply_steps, list(reversed(chain[1:])), "downgrade")
    await connection.run_sync(apply_steps, chain[1:], "upgrade")
    assert (
        await connection.scalar(
            sa.text("SELECT tenant_code FROM sys_tenant WHERE tenant_id=0")
        )
        == "default"
    )


async def test_setting_split_preserves_tenant_values_and_custom_parameters(
    migration_connection,
):
    connection = migration_connection
    chain = release_chain()
    await connection.run_sync(apply_steps, chain[:-1], "upgrade")
    await connection.execute(
        sa.text(
            "INSERT INTO sys_tenant (tenant_id, tenant_code, tenant_name, status, lifecycle_state) "
            "VALUES (42, 'setting-test', 'Settings test', '2', 'prepared')"
        )
    )
    original = [
        {
            "id": 501,
            "tenant": 0,
            "key": "site_name",
            "value": "Existing brand",
            "status": "1",
        },
        {
            "id": 502,
            "tenant": 42,
            "key": "site_name",
            "value": "Tenant brand",
            "status": "1",
        },
        {
            "id": 503,
            "tenant": 42,
            "key": "ai:enabled_tools",
            "value": '["custom.tool"]',
            "status": "2",
        },
        {
            "id": 504,
            "tenant": 42,
            "key": "business:region",
            "value": "west",
            "status": "1",
        },
    ]
    await connection.execute(
        sa.text(
            "INSERT INTO sys_config (config_id, tenant_id, config_name, config_key, config_value, config_type, config_group, status, is_public) "
            "VALUES (:id, :tenant, :key, :key, :value, 'text', 'basic', :status, false)"
        ),
        original,
    )
    await connection.run_sync(apply_steps, chain[-1:], "upgrade")
    rows = (
        await connection.execute(
            sa.text(
                "SELECT setting_id, tenant_id, setting_key, setting_value, status FROM sys_setting ORDER BY setting_id"
            )
        )
    ).all()
    assert [tuple(row) for row in rows] == [tuple(row.values()) for row in original[:3]]
    assert (
        await connection.execute(
            sa.text("SELECT config_key, config_value FROM sys_config")
        )
    ).all() == [("business:region", "west")]
    # Rollback must preserve edits made after the split, not resurrect the old value.
    await connection.execute(
        sa.text("UPDATE sys_setting SET setting_value=:value WHERE setting_id=501"),
        {"value": "Updated brand"},
    )
    await connection.run_sync(apply_steps, chain[-1:], "downgrade")
    assert (
        await connection.scalar(
            sa.text("SELECT config_value FROM sys_config WHERE config_id=501")
        )
        == "Updated brand"
    )
    assert (
        await connection.scalar(
            sa.text("SELECT is_public FROM sys_config WHERE config_id=501")
        )
        is True
    )
    await connection.run_sync(apply_steps, chain[-1:], "upgrade")
    assert (
        await connection.scalar(
            sa.text("SELECT setting_value FROM sys_setting WHERE setting_id=501")
        )
        == "Updated brand"
    )
    assert await connection.scalar(sa.text("SELECT count(*) FROM sys_setting")) == 3


async def test_release_boundary_data_survives_upgrade(migration_connection):
    connection = migration_connection
    chain = release_chain()
    await connection.run_sync(apply_steps, chain[:1], "upgrade")
    await connection.execute(
        sa.text(
            "INSERT INTO sys_role (role_id, role_name, role_code, status) "
            "VALUES (1, '超级管理员', 'R_SUPER', '1')"
        )
    )
    for statement in (
        "INSERT INTO ai_provider "
        "(provider_id, provider_code, name, api_key, is_enabled) "
        "VALUES (1, 'openai', 'Existing provider', 'test-only', true)",
        "INSERT INTO ai_model "
        "(model_id, provider_id, name, capabilities, is_enabled, sort_order) "
        "VALUES (1, 1, 'existing-model', '[]', true, 1), "
        "(2, 1, 'disabled-model', '[]', false, 0), "
        "(3, 1, 'another-model', '[]', true, 2)",
        "INSERT INTO sys_menu "
        '(menu_id, parent_id, menu_name, menu_type, "order", status) '
        "VALUES (1, 0, 'Existing root', '1', 1, '1')",
        "INSERT INTO sys_user (user_id, user_name, hashed_password, status) "
        "VALUES (1, 'existing-user', 'test-only', '1')",
        "INSERT INTO sys_login_log (login_log_id, user_id, username, status) "
        "VALUES (1, 1, 'existing-user', '1'), (2, NULL, 'unknown-user', '2')",
    ):
        await connection.execute(sa.text(statement))
    await connection.run_sync(apply_steps, chain[1:], "upgrade")
    assert (
        await connection.scalar(
            sa.text("SELECT parent_id FROM sys_menu WHERE menu_id=1")
        )
    ) is None
    audits = (
        await connection.execute(
            sa.text(
                "SELECT tenant_id, audit_scope FROM sys_login_log ORDER BY login_log_id"
            )
        )
    ).all()
    assert [tuple(row) for row in audits] == [(0, "tenant"), (None, "unresolved")]
    policies = (
        await connection.execute(
            sa.text(
                "SELECT tenant_id, model_id, enabled, is_default "
                "FROM tenant_ai_model_policy ORDER BY model_id"
            )
        )
    ).all()
    assert [tuple(row) for row in policies] == [(0, 1, True, True), (0, 3, True, False)]
    row = (
        await connection.execute(
            sa.text(
                "SELECT role_id, role_name, tenant_id FROM sys_role WHERE role_id=1"
            )
        )
    ).one()
    assert tuple(row) == (1, "系统超级管理员", 0)
    await connection.execute(
        sa.text(
            "UPDATE sys_tenant SET status='2', lifecycle_state='disabled' WHERE tenant_id=0"
        )
    )
    assert (
        await connection.scalar(
            sa.text("SELECT row_version FROM sys_tenant WHERE tenant_id=0")
        )
        == 2
    )
