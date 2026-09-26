"""Separate system-owned settings without resetting tenant values."""

import sqlalchemy as sa
from alembic import op

revision = "c926ae092601"
down_revision = "b728ae092401"
branch_labels = None
depends_on = None

# Frozen ownership at this revision; never import the runtime catalog here.
BUILTIN_KEYS = (
    "site_name",
    "site_logo",
    "site_description",
    "site_icp",
    "site_copyright",
    "user_agreement",
    "privacy_policy",
    "default_avatar",
    "register_enabled",
    "user_require_primary_dept",
    "auth:default_password",
    "default_locale",
)
PUBLIC_KEYS = (
    "site_name",
    "site_logo",
    "site_description",
    "site_icp",
    "site_copyright",
    "user_agreement",
    "privacy_policy",
    "default_locale",
)


def upgrade():
    op.create_table(
        "sys_setting",
        sa.Column("setting_id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            sa.ForeignKey("sys_tenant.tenant_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("setting_key", sa.String(100), nullable=False),
        sa.Column("setting_value", sa.Text(), nullable=False),
        sa.Column("status", sa.String(2), nullable=False, server_default="1"),
        sa.Column(
            "create_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "update_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "setting_key", name="uq_sys_setting_tenant_key"
        ),
    )
    connection = op.get_bind()
    owned = "config_key IN :keys OR config_key LIKE 'ai:%' OR config_key LIKE 'security:%' OR config_key LIKE 'upload:%'"
    connection.execute(
        sa.text(
            "INSERT INTO sys_setting (setting_id, tenant_id, setting_key, setting_value, status) "
            "SELECT config_id, tenant_id, config_key, config_value, COALESCE(status, '1') FROM sys_config WHERE "
            + owned
        ).bindparams(sa.bindparam("keys", expanding=True)),
        {"keys": BUILTIN_KEYS},
    )
    connection.execute(
        sa.text("DELETE FROM sys_config WHERE " + owned).bindparams(
            sa.bindparam("keys", expanding=True)
        ),
        {"keys": BUILTIN_KEYS},
    )
    # The old menu now represents only custom parameters; preserve customized names.
    connection.execute(
        sa.text(
            "UPDATE sys_menu SET menu_name='自定义参数' WHERE route_name='system_config' AND menu_name='系统设置'"
        )
    )


def downgrade():
    # No user CRUD can claim a reserved key while this revision is active.
    op.get_bind().execute(
        sa.text(
            "INSERT INTO sys_config (config_id, tenant_id, config_name, config_key, config_value, config_type, config_group, status, is_public) "
            "SELECT setting_id, tenant_id, setting_key, setting_key, setting_value, 'text', 'system', status, setting_key IN :public_keys FROM sys_setting "
            "ON CONFLICT (tenant_id, config_key) DO UPDATE SET config_value=EXCLUDED.config_value, status=EXCLUDED.status"
        ).bindparams(sa.bindparam("public_keys", expanding=True)),
        {"public_keys": PUBLIC_KEYS},
    )
    op.drop_table("sys_setting")
