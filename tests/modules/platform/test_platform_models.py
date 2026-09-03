from sqlalchemy import CheckConstraint, Index

from app.modules.platform.models import PlatformAuditLog, PlatformPrincipal


def test_platform_models_are_global_and_do_not_reuse_tenant_identity():
    principal = PlatformPrincipal.__table__
    audit = PlatformAuditLog.__table__

    assert "tenant_id" not in principal.c
    assert "tenant_id" not in audit.c
    assert "target_tenant_id" in audit.c
    assert principal.c.permissions.nullable is False
    assert audit.c.authorization_audit_id.nullable is True


def test_platform_schema_declares_authorization_lineage_and_access_paths():
    audit = PlatformAuditLog.__table__
    constraints = {
        item.name for item in audit.constraints if isinstance(item, CheckConstraint)
    }
    indexes = {item.name for item in audit.indexes if isinstance(item, Index)}

    assert "ck_platform_audit_event_type" in constraints
    assert "ck_platform_audit_authorization_lineage" in constraints
    assert "ck_platform_audit_event_fields" in constraints
    assert "ck_platform_audit_summary_objects" in constraints
    assert "ck_platform_audit_required_context" in constraints
    assert "ix_platform_audit_correlation" in indexes
    assert "ix_platform_audit_actor_time" in indexes
    assert "uq_platform_audit_one_completion" in indexes
