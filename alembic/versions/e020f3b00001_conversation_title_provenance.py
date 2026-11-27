"""Preserve unknown historic titles; authored titles are explicit new facts."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e020f3b00001"
down_revision = "e020f3a00001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ai_conversation", sa.Column("title_origin", sa.String(16), nullable=False, server_default="legacy_unknown"))
    op.create_check_constraint("ck_ai_conversation_title_origin", "ai_conversation", "title_origin IN ('legacy_unknown', 'authored', 'fixed')")
    for table in ("ai_prepared_action", "ai_operation_log"):
        op.add_column(table, sa.Column("result_field_authority", postgresql.JSONB(none_as_null=True), nullable=True))
        op.create_check_constraint("ck_" + table + "_result_authority", table, "result_field_authority IS NULL OR (jsonb_typeof(result_field_authority) = 'object' AND pg_column_size(result_field_authority) <= 4194304)")


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM ai_conversation WHERE title_origin <> 'legacy_unknown') OR EXISTS(SELECT 1 FROM sys_field_policy_head WHERE ever_enforced) OR EXISTS(SELECT 1 FROM ai_prepared_action WHERE result_field_authority IS NOT NULL) OR EXISTS(SELECT 1 FROM ai_operation_log WHERE result_field_authority IS NOT NULL)")):
        raise RuntimeError("FIELD_POLICY_DOWNGRADE_BLOCKED: restore a consistent backup before discarding title provenance")
    op.drop_constraint("ck_ai_conversation_title_origin", "ai_conversation", type_="check")
    op.drop_column("ai_conversation", "title_origin")
    for table in ("ai_prepared_action", "ai_operation_log"):
        op.drop_constraint("ck_" + table + "_result_authority", table, type_="check")
        op.drop_column(table, "result_field_authority")
