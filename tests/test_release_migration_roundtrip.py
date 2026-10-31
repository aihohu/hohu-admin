"""Execute release migrations in a disposable, transaction-isolated schema."""

from types import SimpleNamespace
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
        "d01010100101",
        "e0202a000001",
        "e0203b000001",
    ]
    return [item.module for item in revisions]


def release_steps():
    """Expose only the six historical boundaries inside the original two revisions."""
    baseline, consolidated = release_chain()[:2]
    return [baseline] + [
        SimpleNamespace(
            upgrade=getattr(consolidated, f"_upgrade_{step}"),
            downgrade=getattr(consolidated, f"_downgrade_{step}"),
        )
        for step in (
            "schema",
            "data_fixes",
            "builtin_translation",
            "system_settings",
            "account_self_service",
        )
    ]


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
        "trg_user_email_trust",
        "trg_ufp_revision_immutable",
        "trg_ufp_audit_immutable",
        "trg_ufp_guard_protection",
        "trg_ufp_state_protection",
        "trg_ufp_binding_protection",
        "trg_ufp_initialize_tenant",
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
    initial_policy = sa.text(
        "SELECT g.guard_id, g.epoch, s.tenant_id, s.mode, s.ever_enforced, s.epoch, "
        "s.candidate_manifest_digest, s.active_manifest_digest "
        "FROM sys_user_field_policy_global_guard g "
        "CROSS JOIN sys_user_field_policy_state s"
    )
    expected_policy = [(1, 1, 0, "legacy", False, 1, None, None)]
    assert (await connection.execute(initial_policy)).all() == expected_policy
    await connection.run_sync(apply_steps, list(reversed(chain[1:])), "downgrade")
    assert (
        await connection.scalar(
            sa.text("SELECT to_regclass('sys_user_field_policy_state')")
        )
        is None
    )
    await connection.run_sync(apply_steps, chain[1:], "upgrade")
    assert (
        await connection.scalar(
            sa.text("SELECT tenant_code FROM sys_tenant WHERE tenant_id=0")
        )
        == "default"
    )
    assert (await connection.execute(initial_policy)).all() == expected_policy
    assert (
        await connection.execute(
            sa.text(
                "SELECT (SELECT count(*) FROM sys_user_field_policy_revision), "
                "(SELECT count(*) FROM sys_user_field_policy_binding), "
                "(SELECT count(*) FROM sys_user_field_policy_audit)"
            )
        )
    ).one() == (0, 0, 0)


async def test_setting_split_preserves_tenant_values_and_custom_parameters(
    migration_connection,
):
    connection = migration_connection
    chain = release_steps()[:-1]  # Seed values immediately before the setting split.
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
        "INSERT INTO sys_config (config_id, config_name, config_key, config_value, config_type, config_group, status) "
        "VALUES (1, 'Registration', 'register_enabled', 'true', 'text', 'basic', '1'), "
        "(2, 'Brand', 'site_name', 'Existing brand', 'text', 'basic', '1'), "
        "(3, 'Region', 'business:region', 'west', 'text', 'basic', '1')",
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
        await connection.execute(
            sa.text(
                "SELECT setting_key, setting_value FROM sys_setting ORDER BY setting_id"
            )
        )
    ).all() == [("register_enabled", "false"), ("site_name", "Existing brand")]
    assert (
        await connection.execute(
            sa.text("SELECT config_key, config_value FROM sys_config")
        )
    ).all() == [("business:region", "west")]
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


async def test_account_upgrade_closes_registration_and_preserves_credentials(
    migration_connection,
):
    connection = migration_connection
    chain = release_steps()
    await connection.run_sync(apply_steps, chain[:-1], "upgrade")
    await connection.execute(
        sa.text(
            "INSERT INTO sys_setting (setting_id, tenant_id, setting_key, setting_value, status) VALUES (77, 0, 'register_enabled', 'true', '1')"
        )
    )
    await connection.execute(
        sa.text(
            "INSERT INTO sys_user (user_id, tenant_id, user_name, hashed_password, user_email, status) VALUES (77, 0, 'existing', 'keep-hash', 'old@example.com', '1')"
        )
    )
    await connection.run_sync(apply_steps, chain[-1:], "upgrade")
    assert (
        await connection.scalar(
            sa.text("SELECT setting_value FROM sys_setting WHERE setting_id=77")
        )
        == "false"
    )
    row = (
        await connection.execute(
            sa.text(
                "SELECT hashed_password, user_email, email_verified_at FROM sys_user WHERE user_id=77"
            )
        )
    ).one()
    assert tuple(row) == ("keep-hash", "old@example.com", None)
    assert (
        await connection.scalar(
            sa.text("SELECT version FROM sys_mail_config WHERE config_id=1")
        )
        == 1
    )
    await connection.execute(
        sa.text("UPDATE sys_user SET email_verified_at=now() WHERE user_id=77")
    )
    await connection.execute(
        sa.text("UPDATE sys_user SET nickname='Changed nickname' WHERE user_id=77")
    )
    assert await connection.scalar(
        sa.text("SELECT email_verified_at IS NOT NULL FROM sys_user WHERE user_id=77")
    )
    await connection.execute(
        sa.text("UPDATE sys_user SET user_email='new@example.com' WHERE user_id=77")
    )
    assert await connection.scalar(
        sa.text("SELECT email_verified_at IS NULL FROM sys_user WHERE user_id=77")
    )


async def test_data_fixes_preserve_custom_roles_and_existing_diagnostics(
    migration_connection,
):
    connection = migration_connection
    chain = release_steps()
    await connection.run_sync(apply_steps, chain[:2], "upgrade")
    await connection.execute(
        sa.text(
            "INSERT INTO sys_role (role_id, tenant_id, role_name, role_code, status) "
            "VALUES (1, 0, 'Custom admin', 'R_SUPER', '1')"
        )
    )
    await connection.execute(
        sa.text(
            "INSERT INTO ai_operation_log (log_id, trace_id, conversation_id, tenant_id, "
            "user_id, tool_name, tool_call_id, args_hash, args_summary, risk_level, "
            "execution_mode, status, error_code, is_security_event) "
            "VALUES (:id, 'migration-test', 1, 0, 1, 'user.list', :call, 'hash', '{}', "
            "'low', 'hitl', :status, :error, false)"
        ),
        [
            {"id": 1, "call": "expired", "status": "expired", "error": None},
            {"id": 2, "call": "known", "status": "expired", "error": "KNOWN_ERROR"},
            {"id": 3, "call": "success", "status": "success", "error": None},
        ],
    )
    await connection.run_sync(apply_steps, chain[2:], "upgrade")
    assert (
        await connection.scalar(
            sa.text("SELECT role_name FROM sys_role WHERE role_id=1")
        )
        == "Custom admin"
    )
    assert (
        await connection.execute(
            sa.text("SELECT error_code FROM ai_operation_log ORDER BY log_id")
        )
    ).all() == [("AI_HITL_EXPIRED",), ("KNOWN_ERROR",), (None,)]
