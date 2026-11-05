"""Store unactivated user field-policy candidates and authorization state.

Revision ID: e0202a000001
Revises: d01010100101

The catalog identity and DDL below are frozen. This migration does not import
the evolving product catalog, create role policies, or activate field controls.
"""

from alembic import op
import sqlalchemy as sa

revision = "e0202a000001"
down_revision = "d01010100101"
branch_labels = None
depends_on = None

INITIAL_CATALOG_REVISION = "user-fields/1"
INITIAL_CATALOG_DIGEST = (
    "da9fcde4c22e1e48165642a47bcdd340c8b1ebc5f629160e1dab45ff1ca80248"
)

_SCHEMA = (
    "CREATE TABLE sys_user_field_policy_global_guard (\n\tguard_id SMALLINT NOT NULL, \n\tepoch BIGINT DEFAULT '1' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (guard_id), \n\tCONSTRAINT ck_ufp_guard_singleton CHECK (guard_id = 1), \n\tCONSTRAINT ck_ufp_guard_epoch CHECK (epoch >= 1)\n)",
    "CREATE TABLE sys_user_field_policy_state (\n\ttenant_id BIGINT NOT NULL, \n\tobject_type VARCHAR(16) DEFAULT 'user' NOT NULL, \n\tmode VARCHAR(16) DEFAULT 'legacy' NOT NULL, \n\tever_enforced BOOLEAN DEFAULT false NOT NULL, \n\tepoch BIGINT DEFAULT '1' NOT NULL, \n\tcatalog_revision VARCHAR(64) NOT NULL, \n\tcatalog_digest CHAR(64) NOT NULL, \n\tcandidate_manifest_digest CHAR(64), \n\tactive_manifest_digest CHAR(64), \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (tenant_id), \n\tCONSTRAINT ck_ufp_state_object CHECK (object_type = 'user'), \n\tCONSTRAINT ck_ufp_state_mode CHECK (mode IN ('legacy', 'ready')), \n\tCONSTRAINT ck_ufp_state_not_enforced CHECK (ever_enforced = false), \n\tCONSTRAINT ck_ufp_state_epoch CHECK (epoch >= 1), \n\tCONSTRAINT ck_ufp_state_catalog_revision CHECK (btrim(catalog_revision) <> ''), \n\tCONSTRAINT ck_ufp_state_catalog_digest CHECK (catalog_digest ~ '^[0-9a-f]{64}$'), \n\tCONSTRAINT ck_ufp_state_candidate_digest CHECK (candidate_manifest_digest IS NULL OR candidate_manifest_digest ~ '^[0-9a-f]{64}$'), \n\tCONSTRAINT ck_ufp_state_no_active_manifest CHECK (active_manifest_digest IS NULL), \n\tCONSTRAINT ck_ufp_state_ready_manifest CHECK (mode <> 'ready' OR candidate_manifest_digest IS NOT NULL), \n\tFOREIGN KEY(tenant_id) REFERENCES sys_tenant (tenant_id) ON DELETE RESTRICT\n)",
    "CREATE INDEX ix_ufp_state_mode_tenant ON sys_user_field_policy_state (mode, tenant_id)",
    "CREATE TABLE sys_user_field_policy_revision (\n\trevision_id BIGINT NOT NULL, \n\ttenant_id BIGINT NOT NULL, \n\trole_id BIGINT NOT NULL, \n\trevision_no BIGINT NOT NULL, \n\tcatalog_revision VARCHAR(64) NOT NULL, \n\tcatalog_digest CHAR(64) NOT NULL, \n\trules JSONB NOT NULL, \n\trules_digest CHAR(64) NOT NULL, \n\tsource_digest CHAR(64), \n\ttemplate_revision VARCHAR(64), \n\tcreated_by_user_id BIGINT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (revision_id), \n\tCONSTRAINT fk_ufp_revision_role FOREIGN KEY(tenant_id, role_id) REFERENCES sys_role (tenant_id, role_id) ON DELETE RESTRICT, \n\tCONSTRAINT uq_ufp_revision_number UNIQUE (tenant_id, role_id, revision_no), \n\tCONSTRAINT uq_ufp_revision_identity UNIQUE (tenant_id, role_id, revision_id), \n\tCONSTRAINT ck_ufp_revision_number CHECK (revision_no >= 1), \n\tCONSTRAINT ck_ufp_revision_catalog_revision CHECK (btrim(catalog_revision) <> ''), \n\tCONSTRAINT ck_ufp_revision_catalog_digest CHECK (catalog_digest ~ '^[0-9a-f]{64}$'), \n\tCONSTRAINT ck_ufp_revision_rules_digest CHECK (rules_digest ~ '^[0-9a-f]{64}$'), \n\tCONSTRAINT ck_ufp_revision_rules_budget CHECK (CASE WHEN jsonb_typeof(rules) = 'array' THEN jsonb_array_length(rules) <= 256 ELSE false END AND octet_length(rules::text) <= 65536), \n\tCONSTRAINT ck_ufp_revision_source_pair CHECK ((source_digest IS NULL AND template_revision IS NULL) OR (source_digest IS NOT NULL AND template_revision IS NOT NULL AND source_digest ~ '^[0-9a-f]{64}$' AND btrim(template_revision) <> '')), \n\tCONSTRAINT ck_ufp_revision_actor CHECK (created_by_user_id > 0)\n)",
    "CREATE TABLE sys_user_field_policy_binding (\n\ttenant_id BIGINT NOT NULL, \n\trole_id BIGINT NOT NULL, \n\trevision_id BIGINT NOT NULL, \n\tversion BIGINT NOT NULL, \n\tarchived_at TIMESTAMP WITH TIME ZONE, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (tenant_id, role_id), \n\tCONSTRAINT fk_ufp_binding_role FOREIGN KEY(tenant_id, role_id) REFERENCES sys_role (tenant_id, role_id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_ufp_binding_revision FOREIGN KEY(tenant_id, role_id, revision_id) REFERENCES sys_user_field_policy_revision (tenant_id, role_id, revision_id) ON DELETE RESTRICT, \n\tCONSTRAINT ck_ufp_binding_version CHECK (version >= 1)\n)",
    "CREATE TABLE sys_user_field_policy_audit (\n\taudit_id BIGINT NOT NULL, \n\ttenant_id BIGINT NOT NULL, \n\trole_id BIGINT, \n\tactor_user_id BIGINT NOT NULL, \n\taction VARCHAR(32) NOT NULL, \n\treason VARCHAR(500) NOT NULL, \n\tcorrelation_id VARCHAR(128) NOT NULL, \n\tbefore_digest CHAR(64), \n\tafter_digest CHAR(64) NOT NULL, \n\trevision_id BIGINT, \n\tbefore_version BIGINT NOT NULL, \n\tafter_version BIGINT NOT NULL, \n\tepoch BIGINT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (audit_id), \n\tCONSTRAINT fk_ufp_audit_revision FOREIGN KEY(tenant_id, role_id, revision_id) REFERENCES sys_user_field_policy_revision (tenant_id, role_id, revision_id) ON DELETE RESTRICT, \n\tCONSTRAINT ck_ufp_audit_action CHECK (action IN ('publish', 'archive', 'ready')), \n\tCONSTRAINT ck_ufp_audit_actor CHECK (actor_user_id > 0), \n\tCONSTRAINT ck_ufp_audit_reason CHECK (btrim(reason) <> ''), \n\tCONSTRAINT ck_ufp_audit_correlation CHECK (btrim(correlation_id) <> ''), \n\tCONSTRAINT ck_ufp_audit_before_digest CHECK (before_digest IS NULL OR before_digest ~ '^[0-9a-f]{64}$'), \n\tCONSTRAINT ck_ufp_audit_after_digest CHECK (after_digest ~ '^[0-9a-f]{64}$'), \n\tCONSTRAINT ck_ufp_audit_revision_role CHECK (revision_id IS NULL OR role_id IS NOT NULL), \n\tCONSTRAINT ck_ufp_audit_action_target CHECK ((action = 'ready' AND role_id IS NULL AND revision_id IS NULL) OR (action IN ('publish', 'archive') AND role_id IS NOT NULL AND revision_id IS NOT NULL)), \n\tCONSTRAINT ck_ufp_audit_versions CHECK (before_version >= 0 AND after_version >= 1 AND after_version > before_version), \n\tCONSTRAINT ck_ufp_audit_epoch CHECK (epoch >= 1), \n\tFOREIGN KEY(tenant_id) REFERENCES sys_tenant (tenant_id) ON DELETE RESTRICT\n)",
    "CREATE INDEX ix_ufp_audit_tenant_actor ON sys_user_field_policy_audit (tenant_id, actor_user_id, audit_id)",
    "CREATE INDEX ix_ufp_audit_tenant_role ON sys_user_field_policy_audit (tenant_id, role_id, audit_id)",
)

_PROTECTIONS = (
    """
    CREATE FUNCTION reject_user_field_policy_history_mutation()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'user field policy history is append-only'
            USING ERRCODE = '55000';
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER trg_ufp_revision_immutable
    BEFORE UPDATE OR DELETE ON sys_user_field_policy_revision
    FOR EACH ROW EXECUTE FUNCTION reject_user_field_policy_history_mutation()
    """,
    """
    CREATE TRIGGER trg_ufp_audit_immutable
    BEFORE UPDATE OR DELETE ON sys_user_field_policy_audit
    FOR EACH ROW EXECUTE FUNCTION reject_user_field_policy_history_mutation()
    """,
    """
    CREATE FUNCTION protect_user_field_policy_guard()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'user field policy guard cannot be deleted'
                USING ERRCODE = '55000';
        END IF;
        IF NEW.guard_id IS DISTINCT FROM OLD.guard_id
           OR NEW.created_at IS DISTINCT FROM OLD.created_at
           OR NEW.epoch < OLD.epoch THEN
            RAISE EXCEPTION 'user field policy guard identity/version is invalid'
                USING ERRCODE = '23514';
        END IF;
        NEW.updated_at := now();
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER trg_ufp_guard_protection
    BEFORE UPDATE OR DELETE ON sys_user_field_policy_global_guard
    FOR EACH ROW EXECUTE FUNCTION protect_user_field_policy_guard()
    """,
    """
    CREATE FUNCTION protect_user_field_policy_state()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'user field policy state cannot be deleted'
                USING ERRCODE = '55000';
        END IF;
        IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
           OR NEW.object_type IS DISTINCT FROM OLD.object_type
           OR NEW.created_at IS DISTINCT FROM OLD.created_at
           OR NEW.epoch < OLD.epoch THEN
            RAISE EXCEPTION 'user field policy state identity/version is invalid'
                USING ERRCODE = '23514';
        END IF;
        IF (NEW.mode IS DISTINCT FROM OLD.mode
            OR NEW.ever_enforced IS DISTINCT FROM OLD.ever_enforced
            OR NEW.catalog_revision IS DISTINCT FROM OLD.catalog_revision
            OR NEW.catalog_digest IS DISTINCT FROM OLD.catalog_digest
            OR NEW.candidate_manifest_digest IS DISTINCT FROM OLD.candidate_manifest_digest
            OR NEW.active_manifest_digest IS DISTINCT FROM OLD.active_manifest_digest)
           AND NEW.epoch <= OLD.epoch THEN
            RAISE EXCEPTION 'user field policy state change requires a newer epoch'
                USING ERRCODE = '23514';
        END IF;
        NEW.updated_at := now();
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER trg_ufp_state_protection
    BEFORE UPDATE OR DELETE ON sys_user_field_policy_state
    FOR EACH ROW EXECUTE FUNCTION protect_user_field_policy_state()
    """,
    """
    CREATE FUNCTION protect_user_field_policy_binding()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'user field policy binding cannot be deleted'
                USING ERRCODE = '55000';
        END IF;
        IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
           OR NEW.role_id IS DISTINCT FROM OLD.role_id
           OR NEW.created_at IS DISTINCT FROM OLD.created_at
           OR NEW.version < OLD.version THEN
            RAISE EXCEPTION 'user field policy binding identity/version is invalid'
                USING ERRCODE = '23514';
        END IF;
        IF (NEW.revision_id IS DISTINCT FROM OLD.revision_id
            OR NEW.archived_at IS DISTINCT FROM OLD.archived_at)
           AND NEW.version <= OLD.version THEN
            RAISE EXCEPTION 'user field policy binding change requires a newer version'
                USING ERRCODE = '23514';
        END IF;
        NEW.updated_at := now();
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER trg_ufp_binding_protection
    BEFORE UPDATE OR DELETE ON sys_user_field_policy_binding
    FOR EACH ROW EXECUTE FUNCTION protect_user_field_policy_binding()
    """,
    """
    CREATE FUNCTION initialize_user_field_policy_state()
    RETURNS trigger AS $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM sys_user_field_policy_global_guard WHERE guard_id = 1
        ) THEN
            RAISE EXCEPTION 'user field policy guard is unavailable'
                USING ERRCODE = '55000';
        END IF;
        INSERT INTO sys_user_field_policy_state
            (tenant_id, object_type, mode, ever_enforced, epoch,
             catalog_revision, catalog_digest)
        VALUES
            (NEW.tenant_id, 'user', 'legacy', false, 1, 'user-fields/1',
             'da9fcde4c22e1e48165642a47bcdd340c8b1ebc5f629160e1dab45ff1ca80248');
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER trg_ufp_initialize_tenant
    AFTER INSERT ON sys_tenant
    FOR EACH ROW EXECUTE FUNCTION initialize_user_field_policy_state()
    """,
)

_DOWNGRADE_GUARD = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM sys_user_field_policy_revision)
       OR EXISTS (SELECT 1 FROM sys_user_field_policy_binding)
       OR EXISTS (SELECT 1 FROM sys_user_field_policy_audit)
       OR EXISTS (
           SELECT 1 FROM sys_user_field_policy_state
           WHERE mode <> 'legacy' OR ever_enforced OR epoch <> 1
              OR candidate_manifest_digest IS NOT NULL
              OR active_manifest_digest IS NOT NULL
              OR catalog_revision <> 'user-fields/1'
              OR catalog_digest <> 'da9fcde4c22e1e48165642a47bcdd340c8b1ebc5f629160e1dab45ff1ca80248'
       )
       OR (SELECT count(*) FROM sys_user_field_policy_global_guard
           WHERE guard_id = 1 AND epoch = 1) <> 1 THEN
        RAISE EXCEPTION 'user field policy history/state prevents destructive downgrade'
            USING ERRCODE = '55000';
    END IF;
END;
$$
"""


def upgrade() -> None:
    for statement in _SCHEMA:
        op.execute(sa.text(statement))
    op.execute(
        sa.text("INSERT INTO sys_user_field_policy_global_guard (guard_id) VALUES (1)")
    )
    op.execute(
        sa.text(
            """
            INSERT INTO sys_user_field_policy_state
                (tenant_id, object_type, mode, ever_enforced, epoch,
                 catalog_revision, catalog_digest)
            SELECT tenant_id, 'user', 'legacy', false, 1, 'user-fields/1',
                   'da9fcde4c22e1e48165642a47bcdd340c8b1ebc5f629160e1dab45ff1ca80248'
            FROM sys_tenant
            """
        )
    )
    for statement in _PROTECTIONS:
        op.execute(sa.text(statement))


def downgrade() -> None:
    # Maintenance-only: reject history destruction before removing any object.
    op.execute(sa.text(_DOWNGRADE_GUARD))
    op.execute(sa.text("DROP TRIGGER trg_ufp_initialize_tenant ON sys_tenant"))
    op.execute(sa.text("DROP FUNCTION initialize_user_field_policy_state()"))
    op.drop_table("sys_user_field_policy_audit")
    op.drop_table("sys_user_field_policy_binding")
    op.drop_table("sys_user_field_policy_revision")
    op.drop_table("sys_user_field_policy_state")
    op.drop_table("sys_user_field_policy_global_guard")
    op.execute(sa.text("DROP FUNCTION reject_user_field_policy_history_mutation()"))
    op.execute(sa.text("DROP FUNCTION protect_user_field_policy_binding()"))
    op.execute(sa.text("DROP FUNCTION protect_user_field_policy_state()"))
    op.execute(sa.text("DROP FUNCTION protect_user_field_policy_guard()"))
