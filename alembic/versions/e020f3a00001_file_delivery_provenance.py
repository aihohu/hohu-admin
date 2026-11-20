"""Keep historical files unknown; bind new content to explicit delivery facts."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "e020f3a00001"
down_revision = "e020f2c00001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sys_file", sa.Column("delivery_class", sa.String(24), nullable=False, server_default="legacy-unknown"))
    op.add_column("sys_file", sa.Column("content_sha256", sa.CHAR(64), nullable=True))
    op.add_column("sys_file", sa.Column("source_manifest", JSONB(none_as_null=True), nullable=True))
    op.create_check_constraint(
        "ck_sys_file_delivery_proof", "sys_file",
        "(delivery_class = 'legacy-unknown' AND content_sha256 IS NULL AND source_manifest IS NULL) OR "
        "(delivery_class IN ('owned-private','public-image','ai-chat-private','ai-chat-image','user-import') "
        "AND content_sha256 IS NOT NULL AND content_sha256 ~ '^[0-9a-f]{64}$' "
        "AND source_manifest IS NOT NULL AND jsonb_typeof(source_manifest) = 'object')",
    )
    op.create_index("ix_sys_file_delivery", "sys_file", ("tenant_id", "delivery_class", "del_flag"))
    op.create_index("ix_sys_file_public_url", "sys_file", ("file_url", "delivery_class"))


def downgrade():
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM sys_file WHERE delivery_class <> 'legacy-unknown') OR EXISTS(SELECT 1 FROM sys_field_policy_head WHERE ever_enforced)")):
        raise RuntimeError("FIELD_POLICY_DOWNGRADE_BLOCKED: restore the consistent backup before discarding file provenance")
    op.drop_index("ix_sys_file_public_url", table_name="sys_file")
    op.drop_index("ix_sys_file_delivery", table_name="sys_file")
    op.drop_constraint("ck_sys_file_delivery_proof", "sys_file", type_="check")
    op.drop_column("sys_file", "source_manifest")
    op.drop_column("sys_file", "content_sha256")
    op.drop_column("sys_file", "delivery_class")
