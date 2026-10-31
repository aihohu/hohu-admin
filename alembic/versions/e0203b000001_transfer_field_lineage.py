"""Record transfer field provenance without certifying historical assets.

Revision ID: e0203b000001
Revises: e0202a000001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e0203b000001"
down_revision = "e0202a000001"
branch_labels = None
depends_on = None

_TABLES = (
    ("sys_user_import_batch", "ck_import_field_lineage_object_size"),
    ("sys_user_export_task", "ck_export_field_lineage_object_size"),
)


def upgrade():
    for table, constraint in _TABLES:
        op.add_column(table, sa.Column("field_lineage", postgresql.JSONB(), nullable=True))
        op.create_check_constraint(
            constraint,
            table,
            "field_lineage IS NULL OR (jsonb_typeof(field_lineage) = 'object' "
            "AND octet_length(field_lineage::text) <= 2097152)",
        )


def downgrade():
    for table, constraint in reversed(_TABLES):
        op.drop_constraint(constraint, table, type_="check")
        op.drop_column(table, "field_lineage")
