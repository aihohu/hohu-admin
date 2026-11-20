"""Persist verified same-tenant leader provenance without guessing historical names."""

from alembic import op
import sqlalchemy as sa

revision = "e020f2b00001"
down_revision = "e020f2a00001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sys_dept", sa.Column("leader_user_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key("fk_sys_dept_tenant_leader", "sys_dept", "sys_user",
                          ["tenant_id", "leader_user_id"], ["tenant_id", "user_id"], ondelete="RESTRICT")
    op.create_check_constraint("ck_sys_dept_leader_source", "sys_dept",
                               "leader_user_id IS NULL OR NULLIF(btrim(leader), '') IS NOT NULL")
    op.create_index("ix_sys_dept_tenant_leader", "sys_dept", ["tenant_id", "leader_user_id"])


def downgrade():
    connection = op.get_bind()
    if sa.inspect(connection).has_table("sys_field_policy_object_state") and connection.execute(sa.text(
        "SELECT 1 FROM sys_field_policy_object_state WHERE object_id='system.dept' AND ever_enforced LIMIT 1"
    )).first():
        raise RuntimeError("FIELD_POLICY_DOWNGRADE_BLOCKED: restore the consistent pre-enforcement backup")
    op.drop_index("ix_sys_dept_tenant_leader", table_name="sys_dept")
    op.drop_constraint("ck_sys_dept_leader_source", "sys_dept", type_="check")
    op.drop_constraint("fk_sys_dept_tenant_leader", "sys_dept", type_="foreignkey")
    op.drop_column("sys_dept", "leader_user_id")
