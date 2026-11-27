"""Public values require both current content and durable publication provenance."""

from sqlalchemy import select

from app.core.exceptions import AuthorizationException
from app.core.field_policy.contracts import limit
from app.core.tenant import TenantLocatorContext
from app.modules.system.field_policy.control_values import (
    PUBLIC_TEXT_SCHEMA,
    ConfigContent,
    PublicationFact,
    publication_matches,
)
from app.modules.system.models.config import Config
from app.modules.system.models.config_publication import ConfigPublication
from app.modules.system.models.tenant import Tenant
from app.modules.system.service.authorization_read_budget import (
    authorization_read_phase,
)
from app.modules.system.service.parameter_context import (
    custom_config_scope,
    value_ceiling,
)


async def require_public_locator(db, tx, tenant):
    if type(tenant) is not TenantLocatorContext:
        raise AuthorizationException("Invalid public tenant")
    tx.assert_session(db)
    tx.assert_barrier(tenant.tenant_id)
    with authorization_read_phase(db, "identity"):
        live = (
            await db.execute(
                select(
                    Tenant.tenant_code,
                    Tenant.row_version,
                    Tenant.status,
                    Tenant.lifecycle_state,
                ).where(Tenant.tenant_id == tenant.tenant_id)
            )
        ).one_or_none()
        if live != (tenant.tenant_code, tenant.tenant_version, "1", "active"):
            raise AuthorizationException("Invalid public tenant")


async def public_parameters(db, tx, tenant):
    await require_public_locator(db, tx, tenant)
    config, pub = Config.__table__, ConfigPublication.__table__
    last = 0
    count = 0
    result = {}
    with authorization_read_phase(db, "projection"):
        while True:
            rows = (
                (
                    await db.execute(
                        select(
                            *(config.c[name] for name in ConfigContent.model_fields),
                            pub.c.schema_revision,
                            pub.c.content_digest,
                        )
                        .select_from(
                            config.join(
                                pub,
                                (config.c.tenant_id == pub.c.tenant_id)
                                & (config.c.config_id == pub.c.config_id),
                            )
                        )
                        .where(
                            config.c.tenant_id == tenant.tenant_id,
                            config.c.config_id > last,
                            config.c.is_public.is_(True),
                            config.c.status == "1",
                            config.c.config_type == "text",
                            value_ceiling(config),
                            custom_config_scope(config),
                            pub.c.schema_revision == PUBLIC_TEXT_SCHEMA,
                        )
                        .order_by(config.c.config_id)
                        .limit(256)
                    )
                )
                .mappings()
                .all()
            )
            count += len(rows)
            limit(count <= 10000)
            for row in rows:
                content = ConfigContent(
                    **{name: row[name] for name in ConfigContent.model_fields}
                )
                fact = PublicationFact(
                    tenant_id=row["tenant_id"],
                    config_id=row["config_id"],
                    schema_revision=row["schema_revision"],
                    content_digest=row["content_digest"],
                )
                if publication_matches(content, fact):
                    result[row["config_key"]] = row["config_value"]
            if len(rows) < 256:
                return result
            last = rows[-1]["config_id"]
