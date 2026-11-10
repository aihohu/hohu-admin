"""Generic immutable field policy storage, preserving the physical authorization G/B.

Revision ID: e020f1200001
Revises: e0204a000001
"""

from alembic import op
import sqlalchemy as sa

revision = "e020f1200001"
down_revision = "e0204a000001"
branch_labels = None
depends_on = None

PROTECTIONS = """
CREATE FUNCTION gfp_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'generic field policy fact is immutable' USING ERRCODE = '23514';
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION gfp_state_guard() RETURNS trigger AS $$
DECLARE m record;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'generic policy state cannot be deleted' USING ERRCODE = '23514';
    END IF;
    IF TG_TABLE_NAME = 'sys_field_policy_head' THEN
        IF TG_OP = 'UPDATE' AND (NEW.tenant_id <> OLD.tenant_id OR NEW.head_version < OLD.head_version
            OR (OLD.ever_enforced AND (NOT NEW.ever_enforced OR NEW.rollout_state = 'preparing'))) THEN
            RAISE EXCEPTION 'generic head cannot revert' USING ERRCODE = '23514';
        END IF;
        IF NEW.active_manifest_id IS NOT NULL THEN
            SELECT * INTO m FROM sys_field_policy_manifest WHERE tenant_id = NEW.tenant_id AND manifest_id = NEW.active_manifest_id;
            IF NOT FOUND OR m.status <> 'sealed' OR m.catalog_digest <> NEW.catalog_digest OR m.digest <> NEW.active_manifest_digest THEN
                RAISE EXCEPTION 'active manifest is not sealed or current' USING ERRCODE = '23514';
            END IF;
        END IF;
        IF NEW.candidate_manifest_id IS NOT NULL THEN
            SELECT * INTO m FROM sys_field_policy_manifest WHERE tenant_id = NEW.tenant_id AND manifest_id = NEW.candidate_manifest_id;
            IF NOT FOUND OR m.status <> 'sealed' OR m.catalog_digest <> NEW.catalog_digest OR m.digest <> NEW.candidate_manifest_digest THEN
                RAISE EXCEPTION 'candidate manifest is not sealed or current' USING ERRCODE = '23514';
            END IF;
        END IF;
    ELSIF TG_TABLE_NAME = 'sys_field_policy_object_state' THEN
        IF TG_OP = 'UPDATE' AND (NEW.tenant_id <> OLD.tenant_id OR NEW.object_id <> OLD.object_id
            OR NEW.object_version < OLD.object_version OR (OLD.ever_enforced AND (NOT NEW.ever_enforced OR NEW.mode IN ('legacy','ready')))) THEN
            RAISE EXCEPTION 'object policy cannot revert' USING ERRCODE = '23514';
        END IF;
    ELSE
        IF TG_OP = 'UPDATE' AND (NEW.tenant_id <> OLD.tenant_id OR NEW.role_id <> OLD.role_id OR NEW.object_id <> OLD.object_id
            OR NEW.binding_version <> OLD.binding_version + 1) THEN
            RAISE EXCEPTION 'binding requires next version' USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION gfp_manifest_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'building' THEN
            RAISE EXCEPTION 'manifest must start building' USING ERRCODE = '23514';
        END IF;
    ELSIF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'manifest is historical evidence' USING ERRCODE = '23514';
    ELSIF OLD.status <> 'building' OR NEW.status <> 'sealed' OR (to_jsonb(NEW) - 'status') <> (to_jsonb(OLD) - 'status')
        OR NEW.expected_item_count <> (SELECT count(*) FROM sys_field_policy_manifest_item WHERE manifest_id = NEW.manifest_id AND tenant_id = NEW.tenant_id) THEN
        RAISE EXCEPTION 'manifest cannot be sealed or changed' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION gfp_item_guard() RETURNS trigger AS $$
DECLARE state text;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'manifest item is immutable' USING ERRCODE = '23514';
    END IF;
    SELECT status INTO state FROM sys_field_policy_manifest WHERE manifest_id=NEW.manifest_id AND tenant_id=NEW.tenant_id FOR UPDATE;
    IF NOT FOUND OR state <> 'building' THEN
        RAISE EXCEPTION 'manifest is not building' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION gfp_invalidate_ready() RETURNS trigger AS $$
BEGIN
    IF NEW.epoch IS DISTINCT FROM OLD.epoch THEN
        UPDATE sys_field_policy_head SET ready_manifest_digest=NULL, qualification_digest=NULL,
            ready_g_epoch=NULL, ready_b_epoch=NULL, ready_until=NULL WHERE tenant_id=NEW.tenant_id AND ready_manifest_digest IS NOT NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION gfp_reject_legacy_writer() RETURNS trigger AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM sys_field_policy_head WHERE tenant_id=NEW.tenant_id) THEN
        RAISE EXCEPTION 'generic policy requires current policy writer' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

IMMUTABLE = (
    "catalog_snapshot",
    "catalog_object",
    "policy_revision",
    "policy_audit",
    "policy_request",
    "policy_migration_map",
)


def upgrade():
    for statement in DDL:
        op.execute(sa.text(statement))
    # Each fixed function is a separate statement for asyncpg portability.
    for statement in PROTECTIONS.strip().split("$$ LANGUAGE plpgsql;"):
        if statement.strip():
            op.execute(sa.text(statement + "$$ LANGUAGE plpgsql;"))
    for suffix in IMMUTABLE:
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_gfp_immutable BEFORE UPDATE OR DELETE ON sys_field_"
                + suffix
                + " FOR EACH ROW EXECUTE FUNCTION gfp_immutable()"
            )
        )
    for suffix in ("head", "object_state", "binding"):
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_gfp_state BEFORE INSERT OR UPDATE OR DELETE ON sys_field_policy_"
                + suffix
                + " FOR EACH ROW EXECUTE FUNCTION gfp_state_guard()"
            )
        )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_gfp_manifest BEFORE INSERT OR UPDATE OR DELETE ON sys_field_policy_manifest FOR EACH ROW EXECUTE FUNCTION gfp_manifest_guard()"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_gfp_item BEFORE INSERT OR UPDATE OR DELETE ON sys_field_policy_manifest_item FOR EACH ROW EXECUTE FUNCTION gfp_item_guard()"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_gfp_epoch AFTER UPDATE ON sys_user_field_policy_state FOR EACH ROW EXECUTE FUNCTION gfp_invalidate_ready()"
        )
    )
    for suffix in ("revision", "binding"):
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_gfp_legacy BEFORE INSERT OR UPDATE ON sys_user_field_policy_"
                + suffix
                + " FOR EACH ROW EXECUTE FUNCTION gfp_reject_legacy_writer()"
            )
        )


def downgrade():
    op.execute(
        sa.text("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM sys_field_policy_head) OR EXISTS (SELECT 1 FROM sys_field_catalog_snapshot) THEN
            RAISE EXCEPTION 'generic policy facts require consistent backup recovery';
        END IF;
    END $$""")
    )
    op.execute(sa.text("DROP TRIGGER trg_gfp_epoch ON sys_user_field_policy_state"))
    for suffix in ("revision", "binding"):
        op.execute(
            sa.text("DROP TRIGGER trg_gfp_legacy ON sys_user_field_policy_" + suffix)
        )
    op.drop_constraint(
        "fk_gfp_head_active", "sys_field_policy_head", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_gfp_head_candidate", "sys_field_policy_head", type_="foreignkey"
    )
    for suffix in (
        "policy_migration_map",
        "policy_request",
        "policy_audit",
        "policy_manifest_item",
        "policy_manifest",
        "policy_binding",
        "policy_revision",
        "policy_object_state",
        "policy_head",
        "catalog_object",
        "catalog_snapshot",
    ):
        op.drop_table("sys_field_" + suffix)
    for name in (
        "immutable",
        "state_guard",
        "manifest_guard",
        "item_guard",
        "invalidate_ready",
        "reject_legacy_writer",
    ):
        op.execute(sa.text("DROP FUNCTION gfp_" + name + "()"))


DDL = (
    "CREATE TABLE sys_field_catalog_snapshot (\n\tcatalog_digest CHAR(64) NOT NULL, \n\tschema_version INTEGER NOT NULL, \n\tcatalog_revision VARCHAR(64) NOT NULL, \n\taction_catalog_digest CHAR(64) NOT NULL, \n\tadapter_digest CHAR(64) NOT NULL, \n\tcanonical_document JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (catalog_digest), \n\tCONSTRAINT ck_gfp_catalog_digest CHECK (catalog_digest ~ '^[0-9a-f]{64}$'), \n\tCONSTRAINT ck_gfp_action_catalog_digest CHECK (action_catalog_digest ~ '^[0-9a-f]{64}$'), \n\tCONSTRAINT ck_gfp_adapter_digest CHECK (adapter_digest ~ '^[0-9a-f]{64}$'), \n\tCONSTRAINT ck_gfp_catalog_schema CHECK (schema_version = 2), \n\tCONSTRAINT ck_gfp_catalog_revision CHECK (btrim(catalog_revision) <> ''), \n\tCONSTRAINT ck_gfp_catalog_budget CHECK (jsonb_typeof(canonical_document) = 'object' AND octet_length(canonical_document::text) <= 2097152)\n)",
    "CREATE TABLE sys_field_catalog_object (\n\tcatalog_digest CHAR(64) NOT NULL, \n\tobject_id VARCHAR(96) NOT NULL, \n\tdomain_kind VARCHAR(16) NOT NULL, \n\tschema_digest CHAR(64) NOT NULL, \n\tadapter_version VARCHAR(128) NOT NULL, \n\tPRIMARY KEY (catalog_digest, object_id), \n\tCONSTRAINT uq_gfp_catalog_object_domain UNIQUE (catalog_digest, object_id, domain_kind), \n\tCONSTRAINT ck_gfp_catalog_domain CHECK (domain_kind IN ('tenant','platform','system','public')), \n\tCONSTRAINT ck_gfp_object_name CHECK (object_id ~ '^[A-Za-z_][A-Za-z0-9_.:/-]{0,95}$' AND btrim(adapter_version) <> ''), \n\tCONSTRAINT ck_gfp_schema_digest CHECK (schema_digest ~ '^[0-9a-f]{64}$'), \n\tFOREIGN KEY(catalog_digest) REFERENCES sys_field_catalog_snapshot (catalog_digest) ON DELETE RESTRICT\n)",
    "CREATE TABLE sys_field_policy_head (\n\ttenant_id BIGINT NOT NULL, \n\tcatalog_digest CHAR(64) NOT NULL, \n\thead_version BIGINT DEFAULT '1' NOT NULL, \n\trollout_state VARCHAR(16) DEFAULT 'preparing' NOT NULL, \n\tever_enforced BOOLEAN DEFAULT false NOT NULL, \n\tactive_manifest_id BIGINT, \n\tactive_manifest_digest CHAR(64), \n\tcandidate_manifest_id BIGINT, \n\tcandidate_manifest_digest CHAR(64), \n\tready_manifest_digest CHAR(64), \n\tqualification_digest CHAR(64), \n\tready_g_epoch BIGINT, \n\tready_b_epoch BIGINT, \n\tready_until TIMESTAMP WITH TIME ZONE, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (tenant_id), \n\tFOREIGN KEY(tenant_id) REFERENCES sys_user_field_policy_state (tenant_id) ON DELETE RESTRICT, \n\tCONSTRAINT ck_gfp_head_identity CHECK (tenant_id >= 0 AND head_version >= 1), \n\tCONSTRAINT ck_gfp_head_state CHECK (rollout_state IN ('preparing','enforcing','frozen')), \n\tCONSTRAINT ck_gfp_head_pairs CHECK ((active_manifest_id IS NULL) = (active_manifest_digest IS NULL) AND (candidate_manifest_id IS NULL) = (candidate_manifest_digest IS NULL)), \n\tCONSTRAINT ck_gfp_head_enforced CHECK ((rollout_state = 'preparing' AND NOT ever_enforced AND active_manifest_id IS NULL) OR (rollout_state IN ('enforcing','frozen') AND ever_enforced AND active_manifest_id IS NOT NULL)), \n\tCONSTRAINT ck_gfp_head_ready CHECK ((ready_manifest_digest IS NULL AND qualification_digest IS NULL AND ready_g_epoch IS NULL AND ready_b_epoch IS NULL AND ready_until IS NULL) OR (ready_manifest_digest IS NOT NULL AND qualification_digest IS NOT NULL AND ready_g_epoch IS NOT NULL AND ready_b_epoch IS NOT NULL AND ready_until IS NOT NULL AND candidate_manifest_digest IS NOT NULL AND ready_manifest_digest = candidate_manifest_digest AND ready_g_epoch >= 1 AND ready_b_epoch >= 1)), \n\tCONSTRAINT ck_gfp_ready_manifest_digest CHECK (ready_manifest_digest IS NULL OR (ready_manifest_digest ~ '^[0-9a-f]{64}$')), \n\tCONSTRAINT ck_gfp_qualification_digest CHECK (qualification_digest IS NULL OR (qualification_digest ~ '^[0-9a-f]{64}$')), \n\tFOREIGN KEY(tenant_id) REFERENCES sys_tenant (tenant_id) ON DELETE RESTRICT, \n\tFOREIGN KEY(catalog_digest) REFERENCES sys_field_catalog_snapshot (catalog_digest) ON DELETE RESTRICT\n)",
    "CREATE TABLE sys_field_policy_object_state (\n\ttenant_id BIGINT NOT NULL, \n\tobject_id VARCHAR(96) NOT NULL, \n\tcatalog_digest CHAR(64) NOT NULL, \n\tdomain_kind VARCHAR(16) DEFAULT 'tenant' NOT NULL, \n\tmode VARCHAR(16) DEFAULT 'legacy' NOT NULL, \n\tever_enforced BOOLEAN DEFAULT false NOT NULL, \n\tobject_version BIGINT DEFAULT '1' NOT NULL, \n\tactive_object_digest CHAR(64), \n\tPRIMARY KEY (tenant_id, object_id), \n\tFOREIGN KEY(catalog_digest, object_id, domain_kind) REFERENCES sys_field_catalog_object (catalog_digest, object_id, domain_kind) ON DELETE RESTRICT, \n\tCONSTRAINT ck_gfp_object_identity CHECK (domain_kind = 'tenant' AND object_version >= 1), \n\tCONSTRAINT ck_gfp_object_mode CHECK (mode IN ('legacy','ready','enforcing','frozen')), \n\tCONSTRAINT ck_gfp_object_enforced CHECK ((mode IN ('legacy','ready') AND NOT ever_enforced AND active_object_digest IS NULL) OR (mode IN ('enforcing','frozen') AND ever_enforced AND active_object_digest IS NOT NULL)), \n\tCONSTRAINT ck_gfp_active_object_digest CHECK (active_object_digest IS NULL OR (active_object_digest ~ '^[0-9a-f]{64}$')), \n\tFOREIGN KEY(tenant_id) REFERENCES sys_field_policy_head (tenant_id) ON DELETE RESTRICT\n)",
    "CREATE TABLE sys_field_policy_revision (\n\trevision_id BIGINT NOT NULL, \n\ttenant_id BIGINT NOT NULL, \n\trole_id BIGINT NOT NULL, \n\tobject_id VARCHAR(96) NOT NULL, \n\trevision_no BIGINT NOT NULL, \n\tcatalog_digest CHAR(64) NOT NULL, \n\tdomain_kind VARCHAR(16) DEFAULT 'tenant' NOT NULL, \n\trules JSONB NOT NULL, \n\trules_digest CHAR(64) NOT NULL, \n\tsource_digest CHAR(64), \n\ttemplate_revision VARCHAR(64), \n\tactor_user_id BIGINT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (revision_id), \n\tFOREIGN KEY(tenant_id, role_id) REFERENCES sys_role (tenant_id, role_id) ON DELETE RESTRICT, \n\tFOREIGN KEY(tenant_id, object_id) REFERENCES sys_field_policy_object_state (tenant_id, object_id) ON DELETE RESTRICT, \n\tFOREIGN KEY(catalog_digest, object_id, domain_kind) REFERENCES sys_field_catalog_object (catalog_digest, object_id, domain_kind) ON DELETE RESTRICT, \n\tCONSTRAINT uq_gfp_revision_number UNIQUE (tenant_id, role_id, object_id, revision_no), \n\tCONSTRAINT uq_gfp_revision_identity UNIQUE (tenant_id, role_id, object_id, revision_id), \n\tCONSTRAINT uq_gfp_revision_content UNIQUE (tenant_id, role_id, object_id, revision_id, catalog_digest, rules_digest), \n\tCONSTRAINT ck_gfp_revision_identity CHECK (revision_id > 0 AND role_id > 0 AND revision_no >= 1 AND actor_user_id > 0 AND domain_kind = 'tenant'), \n\tCONSTRAINT ck_gfp_revision_budget CHECK (CASE WHEN jsonb_typeof(rules) = 'array' THEN jsonb_array_length(rules) <= CASE WHEN object_id = 'system.user' THEN 512 ELSE 2048 END ELSE false END AND octet_length(rules::text) <= 524288), \n\tCONSTRAINT ck_gfp_revision_source CHECK ((source_digest IS NULL AND template_revision IS NULL) OR (source_digest IS NOT NULL AND template_revision IS NOT NULL AND btrim(template_revision) <> '')), \n\tCONSTRAINT ck_gfp_rules_digest CHECK (rules_digest ~ '^[0-9a-f]{64}$'), \n\tCONSTRAINT ck_gfp_source_digest CHECK (source_digest IS NULL OR (source_digest ~ '^[0-9a-f]{64}$'))\n)",
    'CREATE TABLE sys_field_policy_binding (\n\ttenant_id BIGINT NOT NULL, \n\trole_id BIGINT NOT NULL, \n\tobject_id VARCHAR(96) NOT NULL, \n\trevision_id BIGINT NOT NULL, \n\tbinding_version BIGINT NOT NULL, \n\tarchived_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (tenant_id, role_id, object_id), \n\tFOREIGN KEY(tenant_id, role_id, object_id, revision_id) REFERENCES sys_field_policy_revision (tenant_id, role_id, object_id, revision_id) ON DELETE RESTRICT, \n\tCONSTRAINT ck_gfp_binding_version CHECK (binding_version >= 1)\n)',
    "CREATE TABLE sys_field_policy_manifest (\n\tmanifest_id BIGINT NOT NULL, \n\ttenant_id BIGINT NOT NULL, \n\tmanifest_version BIGINT NOT NULL, \n\tbase_active_digest CHAR(64), \n\tcatalog_digest CHAR(64) NOT NULL, \n\taction_catalog_digest CHAR(64) NOT NULL, \n\tadapter_digest CHAR(64) NOT NULL, \n\tdigest CHAR(64) NOT NULL, \n\tstatus VARCHAR(16) DEFAULT 'building' NOT NULL, \n\texpected_item_count INTEGER NOT NULL, \n\tcreated_by BIGINT, \n\tcreation_kind VARCHAR(16) DEFAULT 'actor' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (manifest_id), \n\tCONSTRAINT uq_gfp_manifest_identity UNIQUE (tenant_id, manifest_id, digest), \n\tCONSTRAINT uq_gfp_manifest_catalog UNIQUE (tenant_id, manifest_id, catalog_digest), \n\tCONSTRAINT uq_gfp_manifest_version UNIQUE (tenant_id, manifest_version), \n\tCONSTRAINT ck_gfp_manifest_identity CHECK (manifest_id > 0 AND manifest_version >= 1 AND expected_item_count BETWEEN 0 AND 16384 AND ((creation_kind = 'actor' AND created_by IS NOT NULL AND created_by > 0) OR (creation_kind = 'migration' AND created_by IS NULL))), \n\tCONSTRAINT ck_gfp_manifest_status CHECK (status IN ('building','sealed')), \n\tCONSTRAINT ck_gfp_digest CHECK (digest ~ '^[0-9a-f]{64}$'), \n\tCONSTRAINT ck_gfp_base_active_digest CHECK (base_active_digest IS NULL OR (base_active_digest ~ '^[0-9a-f]{64}$')), \n\tCONSTRAINT ck_gfp_action_catalog_digest CHECK (action_catalog_digest ~ '^[0-9a-f]{64}$'), \n\tCONSTRAINT ck_gfp_adapter_digest CHECK (adapter_digest ~ '^[0-9a-f]{64}$'), \n\tFOREIGN KEY(tenant_id) REFERENCES sys_field_policy_head (tenant_id) ON DELETE RESTRICT, \n\tFOREIGN KEY(catalog_digest) REFERENCES sys_field_catalog_snapshot (catalog_digest) ON DELETE RESTRICT\n)",
    'CREATE TABLE sys_field_policy_manifest_item (\n\tmanifest_id BIGINT NOT NULL, \n\trole_id BIGINT NOT NULL, \n\tobject_id VARCHAR(96) NOT NULL, \n\ttenant_id BIGINT NOT NULL, \n\trevision_id BIGINT NOT NULL, \n\tcatalog_digest CHAR(64) NOT NULL, \n\trules_digest CHAR(64) NOT NULL, \n\tPRIMARY KEY (manifest_id, role_id, object_id), \n\tFOREIGN KEY(tenant_id, manifest_id, catalog_digest) REFERENCES sys_field_policy_manifest (tenant_id, manifest_id, catalog_digest) ON DELETE RESTRICT, \n\tFOREIGN KEY(tenant_id, role_id, object_id, revision_id, catalog_digest, rules_digest) REFERENCES sys_field_policy_revision (tenant_id, role_id, object_id, revision_id, catalog_digest, rules_digest) ON DELETE RESTRICT\n)',
    "CREATE TABLE sys_field_policy_audit (\n\taudit_id BIGINT NOT NULL, \n\ttenant_id BIGINT NOT NULL, \n\tactor_user_id BIGINT NOT NULL, \n\trole_id BIGINT, \n\tobject_id VARCHAR(96), \n\taction VARCHAR(16) NOT NULL, \n\treason VARCHAR(500) NOT NULL, \n\trequest_id VARCHAR(36) NOT NULL, \n\tbefore_digest CHAR(64), \n\tafter_digest CHAR(64), \n\tbefore_version BIGINT NOT NULL, \n\tafter_version BIGINT NOT NULL, \n\tg_epoch BIGINT NOT NULL, \n\tb_epoch BIGINT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (audit_id), \n\tCONSTRAINT ck_gfp_audit_values CHECK (audit_id > 0 AND actor_user_id > 0 AND btrim(reason) <> '' AND before_version >= 0 AND after_version > before_version AND g_epoch >= 1 AND b_epoch >= 1), \n\tCONSTRAINT ck_gfp_audit_action CHECK (action IN ('publish','archive','build','ready','activate','freeze','migrate')), \n\tCONSTRAINT ck_gfp_before_digest CHECK (before_digest IS NULL OR (before_digest ~ '^[0-9a-f]{64}$')), \n\tCONSTRAINT ck_gfp_after_digest CHECK (after_digest IS NULL OR (after_digest ~ '^[0-9a-f]{64}$')), \n\tFOREIGN KEY(tenant_id) REFERENCES sys_field_policy_head (tenant_id) ON DELETE RESTRICT\n)",
    "CREATE TABLE sys_field_policy_request (\n\ttenant_id BIGINT NOT NULL, \n\tactor_user_id BIGINT NOT NULL, \n\trequest_id VARCHAR(36) NOT NULL, \n\tcommand_digest CHAR(64) NOT NULL, \n\treceipt JSONB NOT NULL, \n\treceipt_digest CHAR(64) NOT NULL, \n\tmanifest_id BIGINT, \n\tmanifest_digest CHAR(64), \n\trevision_id BIGINT, \n\trole_id BIGINT, \n\tobject_id VARCHAR(96), \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (tenant_id, actor_user_id, request_id), \n\tFOREIGN KEY(tenant_id, role_id, object_id, revision_id) REFERENCES sys_field_policy_revision (tenant_id, role_id, object_id, revision_id) ON DELETE RESTRICT, \n\tCONSTRAINT ck_gfp_request_revision CHECK ((revision_id IS NULL AND role_id IS NULL AND object_id IS NULL) OR (revision_id IS NOT NULL AND role_id IS NOT NULL AND object_id IS NOT NULL)), \n\tFOREIGN KEY(tenant_id, manifest_id, manifest_digest) REFERENCES sys_field_policy_manifest (tenant_id, manifest_id, digest) ON DELETE RESTRICT, \n\tCONSTRAINT ck_gfp_request_id CHECK (actor_user_id > 0 AND request_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'), \n\tCONSTRAINT ck_gfp_request_receipt CHECK ((manifest_id IS NULL) = (manifest_digest IS NULL) AND jsonb_typeof(receipt) = 'object' AND octet_length(receipt::text) <= 16384), \n\tCONSTRAINT ck_gfp_command_digest CHECK (command_digest ~ '^[0-9a-f]{64}$'), \n\tCONSTRAINT ck_gfp_receipt_digest CHECK (receipt_digest ~ '^[0-9a-f]{64}$'), \n\tFOREIGN KEY(tenant_id) REFERENCES sys_field_policy_head (tenant_id) ON DELETE RESTRICT\n)",
    "CREATE TABLE sys_field_policy_migration_map (\n\tlegacy_table VARCHAR(16) NOT NULL, \n\tlegacy_id BIGINT NOT NULL, \n\ttenant_id BIGINT NOT NULL, \n\trole_id BIGINT, \n\ttarget_kind VARCHAR(16) NOT NULL, \n\ttarget_id BIGINT NOT NULL, \n\told_digest CHAR(64) NOT NULL, \n\tnew_digest CHAR(64) NOT NULL, \n\tbatch_id VARCHAR(64) NOT NULL, \n\tsource_build CHAR(40) NOT NULL, \n\tcatalog_digest CHAR(64) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (legacy_table, legacy_id), \n\tCONSTRAINT ck_gfp_migration_source CHECK (legacy_table IN ('revision','audit','state') AND legacy_id >= 0 AND btrim(batch_id) <> '' AND source_build ~ '^[0-9a-f]{40}$'), \n\tCONSTRAINT uq_gfp_migration_target UNIQUE (target_kind, target_id), \n\tCONSTRAINT ck_gfp_old_digest CHECK (old_digest ~ '^[0-9a-f]{64}$'), \n\tCONSTRAINT ck_gfp_new_digest CHECK (new_digest ~ '^[0-9a-f]{64}$'), \n\tFOREIGN KEY(tenant_id) REFERENCES sys_field_policy_head (tenant_id) ON DELETE RESTRICT, \n\tFOREIGN KEY(catalog_digest) REFERENCES sys_field_catalog_snapshot (catalog_digest) ON DELETE RESTRICT\n)",
    'ALTER TABLE sys_field_policy_head ADD CONSTRAINT fk_gfp_head_active FOREIGN KEY(tenant_id, active_manifest_id, active_manifest_digest) REFERENCES sys_field_policy_manifest (tenant_id, manifest_id, digest) ON DELETE RESTRICT',
    'ALTER TABLE sys_field_policy_head ADD CONSTRAINT fk_gfp_head_candidate FOREIGN KEY(tenant_id, candidate_manifest_id, candidate_manifest_digest) REFERENCES sys_field_policy_manifest (tenant_id, manifest_id, digest) ON DELETE RESTRICT',
    'CREATE INDEX ix_gfp_item_tenant_object_role ON sys_field_policy_manifest_item (tenant_id, object_id, role_id)',
    'CREATE INDEX ix_gfp_audit_tenant_id ON sys_field_policy_audit (tenant_id, audit_id)',
    'CREATE INDEX ix_gfp_migration_tenant_source ON sys_field_policy_migration_map (tenant_id, legacy_table, legacy_id)',
)
