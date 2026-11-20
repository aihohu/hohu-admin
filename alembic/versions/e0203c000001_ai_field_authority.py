"""Add bounded AI field evidence without certifying historical records.

Revision ID: e0203c000001
Revises: e0203b000001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e0203c000001"
down_revision = "e0203b000001"
branch_labels = None
depends_on = None

_TABLES = ("ai_message", "ai_prepared_action", "ai_operation_log")


def upgrade():
    for table in _TABLES:
        op.add_column(table, sa.Column("field_authority", postgresql.JSONB(), nullable=True))
        op.create_check_constraint(
            "ck_" + table + "_field_authority", table,
            "field_authority IS NULL OR (jsonb_typeof(field_authority) = 'object' "
            "AND octet_length(field_authority::text) <= 65536)",
        )


def downgrade():
    for table in reversed(_TABLES):
        op.drop_constraint("ck_" + table + "_field_authority", table, type_="check")
        op.drop_column(table, "field_authority")
