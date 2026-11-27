"""Bind role list modes to the immutable user-object revision."""

import sqlalchemy as sa
from alembic import op

revision = "e020ac100001"
down_revision = "e020f3b00001"
branch_labels = depends_on = None

OLD = "CASE WHEN jsonb_typeof(rules) = 'array' THEN jsonb_array_length(rules) <= CASE WHEN object_id = 'system.user' THEN 512 ELSE 2048 END ELSE false END AND octet_length(rules::text) <= 524288"
NEW = "CASE WHEN jsonb_typeof(rules) = 'array' THEN jsonb_array_length(rules) <= CASE WHEN object_id = 'system.user' THEN 512 ELSE 2048 END WHEN object_id = 'system.user' AND jsonb_typeof(rules->'rules') = 'array' THEN jsonb_array_length(rules->'rules') <= 512 AND jsonb_typeof(rules->'list_policy') IS NOT DISTINCT FROM 'object' ELSE false END AND octet_length(rules::text) <= 524288"


def constraint(value):
    op.drop_constraint("ck_gfp_revision_budget", "sys_field_policy_revision", type_="check")
    op.create_check_constraint("ck_gfp_revision_budget", "sys_field_policy_revision", value)


def upgrade():
    constraint(NEW)


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM sys_field_policy_revision WHERE jsonb_typeof(rules) = 'object')")):
        raise RuntimeError("Restore the backup containing the corresponding authorization revision")
    constraint(OLD)
