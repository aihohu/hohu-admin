"""Add independent platform identity and append-only audit.

Revision ID: 2d3e4f5a6b7c
Revises: 1c2d3e4f5a6b
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "2d3e4f5a6b7c"
down_revision: str | None = "1c2d3e4f5a6b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CREATE_APPEND_ONLY_FUNCTION = sa.text(
    """
    CREATE FUNCTION reject_platform_audit_mutation()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'sys_platform_audit_log is append-only'
            USING ERRCODE = '55000';
    END;
    $$ LANGUAGE plpgsql
    """
)


def _assert_downgrade_safe() -> None:
    """Never silently erase platform identities or append-only evidence."""
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "LOCK TABLE sys_platform_principal, sys_platform_audit_log "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )
    has_platform_data = (
        connection.execute(
            sa.text(
                """
            SELECT EXISTS (SELECT 1 FROM sys_platform_principal)
                OR EXISTS (SELECT 1 FROM sys_platform_audit_log)
            """
            )
        )
        .scalar_one()
    )
    if has_platform_data:
        raise RuntimeError(
            "PLAN5A_DOWNGRADE_PLATFORM_DATA_PRESENT: export and explicitly purge "
            "platform principals and audit history before downgrade"
        )


_CREATE_APPEND_ONLY_TRIGGER = sa.text(
    """
    CREATE TRIGGER trg_platform_audit_append_only
    BEFORE UPDATE OR DELETE ON sys_platform_audit_log
    FOR EACH ROW EXECUTE FUNCTION reject_platform_audit_mutation()
    """
)

_CREATE_PRINCIPAL_VERSION_FUNCTION = sa.text(
    """
    CREATE FUNCTION bump_platform_principal_security_version()
    RETURNS trigger AS $$
    BEGIN
        IF NEW.hashed_password IS DISTINCT FROM OLD.hashed_password
           OR NEW.status IS DISTINCT FROM OLD.status
           OR NEW.permissions IS DISTINCT FROM OLD.permissions THEN
            NEW.row_version := OLD.row_version + 1;
        ELSIF NEW.row_version < OLD.row_version THEN
            RAISE EXCEPTION 'platform principal row_version cannot decrease'
                USING ERRCODE = '23514';
        END IF;
        NEW.updated_at := now();
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """
)

_CREATE_PRINCIPAL_VERSION_TRIGGER = sa.text(
    """
    CREATE TRIGGER trg_platform_principal_security_version
    BEFORE UPDATE ON sys_platform_principal
    FOR EACH ROW EXECUTE FUNCTION bump_platform_principal_security_version()
    """
)

_CREATE_LINEAGE_FUNCTION = sa.text(
    """
    CREATE FUNCTION validate_platform_audit_lineage()
    RETURNS trigger AS $$
    BEGIN
        IF NEW.event_type = 'completed' AND NOT EXISTS (
            SELECT 1
            FROM sys_platform_audit_log authorized
            WHERE authorized.audit_id = NEW.authorization_audit_id
              AND authorized.event_type = 'authorized'
              AND authorized.actor_principal_id = NEW.actor_principal_id
              AND authorized.actor_name = NEW.actor_name
              AND authorized.permission = NEW.permission
              AND authorized.method = NEW.method
              AND authorized.path = NEW.path
              AND authorized.reason = NEW.reason
              AND authorized.ticket_id = NEW.ticket_id
              AND authorized.correlation_id = NEW.correlation_id
              AND authorized.target_tenant_id IS NOT DISTINCT FROM NEW.target_tenant_id
        ) THEN
            RAISE EXCEPTION 'platform completion audit lineage is invalid'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """
)

_CREATE_LINEAGE_TRIGGER = sa.text(
    """
    CREATE TRIGGER trg_platform_audit_validate_lineage
    BEFORE INSERT ON sys_platform_audit_log
    FOR EACH ROW EXECUTE FUNCTION validate_platform_audit_lineage()
    """
)


def upgrade() -> None:
    op.create_table(
        "sys_platform_principal",
        sa.Column("principal_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("principal_name", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=2), server_default="1", nullable=False),
        sa.Column(
            "permissions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("row_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('1', '2')", name="ck_platform_principal_status"),
        sa.CheckConstraint(
            "row_version >= 1", name="ck_platform_principal_row_version"
        ),
        sa.CheckConstraint(
            "principal_name = lower(btrim(principal_name))",
            name="ck_platform_principal_normalized_name",
        ),
        sa.CheckConstraint(
            "principal_name ~ '^[a-z][a-z0-9_-]{2,63}$'",
            name="ck_platform_principal_name_format",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(permissions) = 'array'",
            name="ck_platform_principal_permissions_array",
        ),
        sa.PrimaryKeyConstraint("principal_id"),
        sa.UniqueConstraint(
            "principal_name", name="uq_platform_principal_principal_name"
        ),
    )
    op.create_index(
        "ix_platform_principal_status",
        "sys_platform_principal",
        ["status"],
        unique=False,
    )
    op.execute(_CREATE_PRINCIPAL_VERSION_FUNCTION)
    op.execute(_CREATE_PRINCIPAL_VERSION_TRIGGER)

    op.create_table(
        "sys_platform_audit_log",
        sa.Column("audit_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("authorization_audit_id", sa.BigInteger(), nullable=True),
        sa.Column("actor_principal_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_name", sa.String(length=64), nullable=False),
        sa.Column("permission", sa.String(length=96), nullable=False),
        sa.Column("event_type", sa.String(length=16), nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("path", sa.String(length=256), nullable=False),
        sa.Column("target_tenant_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.String(length=256), nullable=True),
        sa.Column("ticket_id", sa.String(length=128), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column(
            "request_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "result_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("ip", sa.String(length=50), nullable=True),
        sa.Column("denial_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('authorized', 'completed', 'denied')",
            name="ck_platform_audit_event_type",
        ),
        sa.CheckConstraint(
            "(event_type = 'completed' AND authorization_audit_id IS NOT NULL) "
            "OR (event_type IN ('authorized', 'denied') "
            "AND authorization_audit_id IS NULL)",
            name="ck_platform_audit_authorization_lineage",
        ),
        sa.CheckConstraint(
            "event_type = 'denied' OR "
            "(reason IS NOT NULL AND btrim(reason) <> '' "
            "AND ticket_id IS NOT NULL AND btrim(ticket_id) <> '' "
            "AND correlation_id IS NOT NULL AND btrim(correlation_id) <> '')",
            name="ck_platform_audit_required_context",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_platform_audit_duration",
        ),
        sa.CheckConstraint(
            "(event_type = 'authorized' AND status_code IS NULL "
            "AND duration_ms IS NULL AND denial_code IS NULL) OR "
            "(event_type = 'completed' AND status_code BETWEEN 100 AND 599 "
            "AND duration_ms IS NOT NULL AND denial_code IS NULL) OR "
            "(event_type = 'denied' AND status_code BETWEEN 400 AND 599 "
            "AND duration_ms IS NULL AND denial_code IS NOT NULL)",
            name="ck_platform_audit_event_fields",
        ),
        sa.CheckConstraint(
            "(request_summary IS NULL OR "
            "jsonb_typeof(request_summary) = 'object') AND "
            "(result_summary IS NULL OR "
            "jsonb_typeof(result_summary) = 'object')",
            name="ck_platform_audit_summary_objects",
        ),
        sa.ForeignKeyConstraint(
            ["actor_principal_id"],
            ["sys_platform_principal.principal_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["authorization_audit_id"],
            ["sys_platform_audit_log.audit_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_tenant_id"],
            ["sys_tenant.tenant_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("audit_id"),
    )
    op.create_index(
        "ix_platform_audit_correlation",
        "sys_platform_audit_log",
        ["correlation_id"],
        unique=False,
    )
    op.create_index(
        "ix_platform_audit_actor_time",
        "sys_platform_audit_log",
        ["actor_principal_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_platform_audit_target_time",
        "sys_platform_audit_log",
        ["target_tenant_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_platform_audit_one_completion",
        "sys_platform_audit_log",
        ["authorization_audit_id"],
        unique=True,
        postgresql_where=sa.text("authorization_audit_id IS NOT NULL"),
    )
    op.execute(_CREATE_LINEAGE_FUNCTION)
    op.execute(_CREATE_LINEAGE_TRIGGER)
    op.execute(_CREATE_APPEND_ONLY_FUNCTION)
    op.execute(_CREATE_APPEND_ONLY_TRIGGER)


def downgrade() -> None:
    _assert_downgrade_safe()
    op.execute("DROP TRIGGER trg_platform_audit_append_only ON sys_platform_audit_log")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_platform_audit_validate_lineage "
        "ON sys_platform_audit_log"
    )
    op.execute("DROP FUNCTION IF EXISTS validate_platform_audit_lineage()")
    op.execute("DROP INDEX IF EXISTS uq_platform_audit_one_completion")
    op.drop_index("ix_platform_audit_target_time", table_name="sys_platform_audit_log")
    op.drop_index("ix_platform_audit_actor_time", table_name="sys_platform_audit_log")
    op.drop_index("ix_platform_audit_correlation", table_name="sys_platform_audit_log")
    op.drop_table("sys_platform_audit_log")
    op.execute("DROP FUNCTION reject_platform_audit_mutation()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_platform_principal_security_version "
        "ON sys_platform_principal"
    )
    op.execute("DROP FUNCTION IF EXISTS bump_platform_principal_security_version()")
    op.drop_index("ix_platform_principal_status", table_name="sys_platform_principal")
    op.drop_table("sys_platform_principal")
