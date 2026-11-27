"""Enable irreversible, audited user field-policy activation.

Revision ID: e0204a000001
Revises: e0203c000001
"""

from alembic import op
import sqlalchemy as sa

revision = "e0204a000001"
down_revision = "e0203c000001"
branch_labels = None
depends_on = None

STATE = "sys_user_field_policy_state"
AUDIT = "sys_user_field_policy_audit"
NEW = (
    (STATE, "ck_ufp_state_mode", "mode IN ('legacy', 'ready', 'enforcing', 'frozen')"),
    (STATE, "ck_ufp_state_enforcement", "(mode IN ('legacy', 'ready') AND ever_enforced = false AND active_manifest_digest IS NULL) OR (mode IN ('enforcing', 'frozen') AND ever_enforced = true AND active_manifest_digest IS NOT NULL AND active_manifest_digest ~ '^[0-9a-f]{64}$')"),
    (STATE, "ck_ufp_state_active_manifest", "mode <> 'enforcing' OR (candidate_manifest_digest IS NOT NULL AND active_manifest_digest = candidate_manifest_digest)"),
    (AUDIT, "ck_ufp_audit_action", "action IN ('publish', 'archive', 'ready', 'activate', 'freeze')"),
    (AUDIT, "ck_ufp_audit_action_target", "(action IN ('ready', 'activate', 'freeze') AND role_id IS NULL AND revision_id IS NULL) OR (action IN ('publish', 'archive') AND role_id IS NOT NULL AND revision_id IS NOT NULL)"),
)
OLD = (
    (STATE, "ck_ufp_state_mode", "mode IN ('legacy', 'ready')"),
    (STATE, "ck_ufp_state_not_enforced", "ever_enforced = false"),
    (STATE, "ck_ufp_state_no_active_manifest", "active_manifest_digest IS NULL"),
    (AUDIT, "ck_ufp_audit_action", "action IN ('publish', 'archive', 'ready')"),
    (AUDIT, "ck_ufp_audit_action_target", "(action = 'ready' AND role_id IS NULL AND revision_id IS NULL) OR (action IN ('publish', 'archive') AND role_id IS NOT NULL AND revision_id IS NOT NULL)"),
)


def upgrade():
    for table, name, _ in OLD:
        op.drop_constraint(name, table, type_="check")
    for table, name, expression in NEW:
        op.create_check_constraint(name, table, expression)
    op.execute(sa.text("""
        CREATE FUNCTION prevent_user_field_policy_reversion() RETURNS trigger AS $$
        BEGIN
            IF OLD.ever_enforced AND (NOT NEW.ever_enforced OR NEW.mode IN ('legacy', 'ready')) THEN
                RAISE EXCEPTION 'activated field policy cannot revert to legacy' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("CREATE TRIGGER trg_ufp_no_reversion BEFORE UPDATE ON sys_user_field_policy_state FOR EACH ROW EXECUTE FUNCTION prevent_user_field_policy_reversion()"))


def downgrade():
    op.execute(sa.text("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM sys_user_field_policy_state WHERE ever_enforced)
               OR EXISTS (SELECT 1 FROM sys_user_field_policy_audit WHERE action IN ('activate', 'freeze')) THEN
                RAISE EXCEPTION 'activated field policy prevents downgrade';
            END IF;
        END $$
    """))
    op.execute(sa.text("DROP TRIGGER trg_ufp_no_reversion ON sys_user_field_policy_state"))
    op.execute(sa.text("DROP FUNCTION prevent_user_field_policy_reversion()"))
    for table, name, _ in NEW:
        op.drop_constraint(name, table, type_="check")
    for table, name, expression in OLD:
        op.create_check_constraint(name, table, expression)
