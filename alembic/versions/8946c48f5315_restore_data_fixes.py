"""Restore the two data-only fixes lost from the pre-squash chain.

Recovers the data portion of the unreleased revisions 7c8d9e0f1a2b (expiry
diagnostics backfill) and 8d9e0f1a2b3c (scope-aware R_SUPER rename) that were
never committed and were dropped during history compaction. The schema portion
of 7c8d9e0f1a2b (timezone-aware import timestamps) is already covered by the
timezone=True column types emitted in the squash_to_head create_table blocks;
its per-deployment timestamp rewrite only applied to databases that no longer
exist on any supported upgrade path.

Revision ID: 8946c48f5315
Revises: e7cc9aa08769
"""

import sqlalchemy as sa
from alembic import op

revision: str = "8946c48f5315"
down_revision: str = "e7cc9aa08769"
branch_labels = None
depends_on = None

_RENAME_UP = """
UPDATE sys_role
SET role_name = CASE WHEN tenant_id = 0 THEN '系统超级管理员' ELSE '租户管理员' END
WHERE role_code = 'R_SUPER' AND role_name = '超级管理员'
"""

_RENAME_DOWN = """
UPDATE sys_role
SET role_name = '超级管理员'
WHERE role_code = 'R_SUPER'
  AND ((tenant_id = 0 AND role_name = '系统超级管理员')
    OR (tenant_id <> 0 AND role_name = '租户管理员'))
"""


def upgrade() -> None:
    op.execute(sa.text(_RENAME_UP))
    op.execute(
        sa.text(
            """
            UPDATE ai_operation_log AS log
            SET error_code = action.error_code
            FROM ai_prepared_action AS action
            WHERE log.tenant_id = action.tenant_id
              AND log.user_id = action.user_id
              AND log.tool_call_id = action.execute_tool_call_id
              AND log.status = 'expired'
              AND NULLIF(log.error_code, '') IS NULL
              AND NULLIF(action.error_code, '') IS NOT NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE ai_operation_log
            SET error_code = 'AI_HITL_EXPIRED'
            WHERE status = 'expired' AND NULLIF(error_code, '') IS NULL
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text(_RENAME_DOWN))
