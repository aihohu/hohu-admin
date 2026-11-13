"""Privacy/publication/nonce facts have explicit database and lock contracts."""

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from app.core.authorization_facts import FACT_COLUMNS
from app.core.authorization_plan import ResourceKey, ResourceKind
from app.modules.job.models.job import SysJobLog
from app.modules.system.models.config import Config
from app.modules.system.models.config_publication import ConfigPublication
from app.modules.system.schemas.config import ConfigOut
from app.modules.system.service.authorization_resource_catalog import (
    resource_order,
    resource_spec,
)


def test_legacy_values_default_to_unknown_without_exposing_classification():
    column = Config.__table__.c.value_sensitivity
    assert not column.nullable and str(column.server_default.arg) == "unknown"
    assert any(
        "value_sensitivity" in str(c.sqltext)
        for c in Config.__table__.constraints
        if isinstance(c, CheckConstraint)
    )

    assert "value_sensitivity" not in ConfigOut.model_fields


def test_publication_has_same_tenant_parent_exact_content_and_utc_provenance():
    table = ConfigPublication.__table__
    assert tuple(table.primary_key.columns.keys()) == ("tenant_id", "config_id")
    foreign = [c for c in table.constraints if isinstance(c, ForeignKeyConstraint)]
    assert len(foreign) == 1 and foreign[0].ondelete == "RESTRICT"
    assert tuple(foreign[0].columns.keys()) == ("tenant_id", "config_id")
    assert table.c.published_at.type.timezone
    assert not {"config_value", "role_ids", "actor_name"} & set(table.c.keys())
    checks = " ".join(
        str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
    )
    assert (
        "publication_version" in checks
        and "published_by_user_id" in checks
        and "content_digest" in checks
    )
    pub = ResourceKey(ResourceKind.CONFIG_PUBLICATION, 0, 123)
    assert resource_spec(pub.kind).table is table
    assert (
        resource_order(ResourceKey(ResourceKind.CONFIG, 0, 123))
        < resource_order(pub)
        < resource_order(ResourceKey(ResourceKind.MAIL_CONFIG, None, 1))
    )


def test_dispatch_nonce_is_unique_per_tenant_and_immutable_after_insert():
    table = SysJobLog.__table__
    assert table.c.dispatch_id.nullable and table.c.dispatch_expires_at.type.timezone
    assert any(
        tuple(c.columns.keys()) == ("tenant_id", "dispatch_id")
        for c in table.constraints
        if isinstance(c, UniqueConstraint)
    )
    assert (
        not {"dispatch_id", "dispatch_expires_at"} & FACT_COLUMNS[ResourceKind.JOB_LOG]
    )
    assert any(
        "dispatch_id" in str(c.sqltext) and "dispatch_expires_at" in str(c.sqltext)
        for c in table.constraints
        if isinstance(c, CheckConstraint)
    )
