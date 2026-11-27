"""Preserve unknown values and durable, tenant-bound publication/dispatch facts."""

from alembic import op
import sqlalchemy as sa

revision = "e020f2c00001"
down_revision = "e020f2b00001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sys_config", sa.Column("value_sensitivity", sa.String(16), nullable=False, server_default="unknown"))
    op.create_check_constraint("ck_sys_config_value_sensitivity", "sys_config", "value_sensitivity IN ('unknown','private','secret')")
    op.create_table("sys_config_publication",
        sa.Column("tenant_id",sa.BigInteger(),nullable=False),
        sa.Column("config_id",sa.BigInteger(),nullable=False),
        sa.Column("schema_revision",sa.String(64),nullable=False),
        sa.Column("content_digest",sa.CHAR(64),nullable=False),
        sa.Column("publication_version",sa.BigInteger(),nullable=False),
        sa.Column("published_by_user_id",sa.BigInteger(),nullable=False),
        sa.Column("published_at",sa.DateTime(timezone=True),nullable=False),
        sa.PrimaryKeyConstraint("tenant_id","config_id"),
        sa.ForeignKeyConstraint(("tenant_id","config_id"),("sys_config.tenant_id","sys_config.config_id"),name="fk_config_publication_tenant_config",ondelete="RESTRICT"),
        sa.CheckConstraint("publication_version >= 1",name="ck_config_publication_version"),
        sa.CheckConstraint("published_by_user_id > 0",name="ck_config_publication_actor"),
        sa.CheckConstraint("content_digest ~ '^[0-9a-f]{64}$'",name="ck_config_publication_digest"),
        sa.CheckConstraint("btrim(schema_revision) <> ''",name="ck_config_publication_schema"))
    op.add_column("sys_job_log",sa.Column("dispatch_id",sa.Uuid(as_uuid=False),nullable=True))
    op.add_column("sys_job_log",sa.Column("dispatch_expires_at",sa.DateTime(timezone=True),nullable=True))
    op.create_unique_constraint("uq_sys_job_log_tenant_dispatch","sys_job_log",["tenant_id","dispatch_id"])
    op.create_check_constraint("ck_sys_job_log_dispatch_pair","sys_job_log","(dispatch_id IS NULL) = (dispatch_expires_at IS NULL)")


def downgrade():
    raise RuntimeError("FIELD_POLICY_DOWNGRADE_BLOCKED: stop all writers and restore the consistent backup; never revive legacy public values or unexpired dispatches")
