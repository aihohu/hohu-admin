"""Track built-in text ownership without rewriting stored display values."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b728ae092401"
down_revision = "8946c48f5315"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("sys_role", "ai_agent"):
        op.add_column(table, sa.Column("i18n_keys", postgresql.JSONB(none_as_null=True), nullable=True))


def downgrade():
    for table in ("ai_agent", "sys_role"):
        op.drop_column(table, "i18n_keys")
