"""Structural empty policy planning for a newly created tenant, never a grant."""

from dataclasses import dataclass

from sqlalchemy import select

from app.core.authorization_plan import NewResource, ResourceKey, ResourceKind
from app.modules.system.field_policy.ordinary_catalog import production_catalog
from app.modules.system.models.field_policy import FieldPolicyHead
from app.modules.system.service.generic_field_policy_service import failure
from app.modules.system.service.generic_field_policy_service import (
    generic_field_policy_service as policies,
)


@dataclass(frozen=True, slots=True)
class EmptyTenantPolicy:
    catalog_digest: str
    objects: tuple[str, ...]
    new: tuple[NewResource, ...]


async def prepare_new_tenant_policy(db, tx, tenant_id):
    tx.assert_session(db)
    tx.assert_planning(tenant_id, exclusive=True)
    digests = tuple(
        await db.scalars(select(FieldPolicyHead.catalog_digest).distinct().limit(2))
    )
    if not digests:
        return None
    catalog = production_catalog()
    if digests != (catalog.digest,):
        raise failure()
    await policies._catalog(db, catalog)
    objects = tuple(
        sorted(
            obj.object_id
            for obj in catalog.objects.values()
            if obj.domain_kind == "tenant"
        )
    )
    keys = (ResourceKey(ResourceKind.FIELD_POLICY_HEAD, tenant_id, tenant_id),) + tuple(
        ResourceKey(ResourceKind.FIELD_POLICY_OBJECT, tenant_id, obj) for obj in objects
    )
    return EmptyTenantPolicy(
        catalog.digest, objects, tuple(NewResource(key) for key in keys)
    )
