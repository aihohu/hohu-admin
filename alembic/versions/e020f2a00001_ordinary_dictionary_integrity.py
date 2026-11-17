"""Atomic dictionary renames and one default item per tenant/type.

Revision ID: e020f2a00001
Revises: e020f1200001
"""

from alembic import op
import sqlalchemy as sa

revision = "e020f2a00001"
down_revision = "e020f1200001"
branch_labels = None
depends_on = None


def upgrade():
    duplicates = op.get_bind().execute(sa.text(
        "SELECT 1 FROM sys_dict_data WHERE is_default = 'Y' "
        "GROUP BY tenant_id, dict_type HAVING count(*) > 1 LIMIT 1"
    )).first()
    if duplicates:
        raise RuntimeError("DICT_DEFAULT_CONFLICT: repair duplicate defaults before upgrade")
    op.drop_constraint("fk_sys_dict_data_tenant_type", "sys_dict_data", type_="foreignkey")
    op.create_foreign_key("fk_sys_dict_data_tenant_type", "sys_dict_data", "sys_dict_type",
        ["tenant_id", "dict_type"], ["tenant_id", "dict_type"], ondelete="RESTRICT", onupdate="CASCADE")
    op.create_index("uq_sys_dict_data_one_default", "sys_dict_data", ["tenant_id", "dict_type"],
        unique=True, postgresql_where=sa.text("is_default = 'Y'"))


def downgrade():
    op.drop_index("uq_sys_dict_data_one_default", table_name="sys_dict_data")
    op.drop_constraint("fk_sys_dict_data_tenant_type", "sys_dict_data", type_="foreignkey")
    op.create_foreign_key("fk_sys_dict_data_tenant_type", "sys_dict_data", "sys_dict_type",
        ["tenant_id", "dict_type"], ["tenant_id", "dict_type"], ondelete="RESTRICT")
