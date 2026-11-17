"""Sealed snapshot verification and atomic transitions; never reads active from bindings."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, tuple_

from app.core.authorization_plan import ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.field_policy.contracts import require
from app.core.field_policy.documents import catalog_parts, digest, load_rules
from app.core.field_policy.qualification import Qualification
from app.modules.system.models.field_policy import FieldPolicyManifest as Manifest
from app.modules.system.models.field_policy import FieldPolicyManifestItem as Item
from app.modules.system.models.field_policy import FieldPolicyRevision as Revision
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard as Guard,
)


def _unavailable():
    return BusinessException(
        503, "通用字段策略未就绪", error_code="FIELD_POLICY_UNAVAILABLE"
    )


async def read_manifest(db, *, tenant_id, manifest_id, expected_digest, catalog):
    row = (
        (
            await db.execute(
                select(Manifest.__table__).where(
                    Manifest.tenant_id == tenant_id, Manifest.manifest_id == manifest_id
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        row is None
        or row["status"] != "sealed"
        or row["catalog_digest"] != catalog.digest
        or row["digest"] != expected_digest
        or (row["action_catalog_digest"], row["adapter_digest"])
        != catalog_parts(catalog)
    ):
        raise _unavailable()
    r, i = Revision.__table__, Item.__table__
    items, grants, last = [], [], (0, "")
    while True:
        page = (
            (
                await db.execute(
                    select(i, r.c.rules)
                    .join(
                        r,
                        (i.c.tenant_id == r.c.tenant_id)
                        & (i.c.role_id == r.c.role_id)
                        & (i.c.object_id == r.c.object_id)
                        & (i.c.revision_id == r.c.revision_id),
                    )
                    .where(
                        i.c.tenant_id == tenant_id,
                        i.c.manifest_id == manifest_id,
                        tuple_(i.c.role_id, i.c.object_id) > last,
                    )
                    .order_by(i.c.role_id, i.c.object_id)
                    .limit(16)
                )
            )
            .mappings()
            .all()
        )
        if not page:
            break
        for item in page:
            if (
                item["catalog_digest"] != catalog.digest
                or digest(item["rules"]) != item["rules_digest"]
            ):
                raise _unavailable()
            grants.extend(
                load_rules(
                    catalog,
                    item["rules"],
                    tenant_id=tenant_id,
                    role_id=item["role_id"],
                    object_id=item["object_id"],
                )
            )
            items.append(
                {
                    "role_id": str(item["role_id"]),
                    "object_id": item["object_id"],
                    "revision_id": str(item["revision_id"]),
                    "rules_digest": item["rules_digest"],
                }
            )
        require(len(items) <= 16384 and len(grants) <= 262144)
        last = (page[-1]["role_id"], page[-1]["object_id"])
    # Keep the codec here to avoid importing the service singleton during startup.
    document = {
        "schema_version": 2,
        "tenant_id": str(tenant_id),
        "base_active_digest": row["base_active_digest"],
        "catalog_digest": catalog.digest,
        "action_catalog_digest": row["action_catalog_digest"],
        "adapter_digest": row["adapter_digest"],
        "objects": sorted(
            obj.object_id
            for obj in catalog.objects.values()
            if obj.domain_kind == "tenant"
        ),
        "items": items,
    }
    if (
        len(items) != row["expected_item_count"]
        or digest(document, 4 * 1024 * 1024) != expected_digest
    ):
        raise _unavailable()
    return row, tuple(items), tuple(grants)


def affected_objects(catalog, object_id):
    require(
        type(object_id) is str
        and object_id in catalog.objects
        and catalog.objects[object_id].domain_kind == "tenant"
    )
    affected = {object_id}
    while True:
        new = {
            obj.object_id
            for obj in catalog.objects.values()
            if obj.domain_kind == "tenant"
            and any(
                source.object_id in affected
                for field in obj.fields
                for source in field.sources
            )
        } - affected
        if not new:
            return affected
        affected.update(new)


async def transition(
    service, db, tx, plan, tenant, catalog, cmd, head, objects, barrier
):
    now = datetime.now(UTC)
    changes, mirror = {}, None
    action = cmd["action"]
    if action == "freeze":
        affected = affected_objects(catalog, cmd["object_id"])
        require(
            any(
                row["object_id"] == cmd["object_id"] and row["ever_enforced"]
                for row in objects
            )
        )
        for row in objects:
            if row["object_id"] in affected and row["ever_enforced"]:
                await tx.update_held(
                    ResourceKey(
                        K.FIELD_POLICY_OBJECT, tenant.tenant_id, row["object_id"]
                    ),
                    {"mode": "frozen", "object_version": row["object_version"] + 1},
                )
                if row["object_id"] == "system.user":
                    mirror = {
                        "mode": "frozen",
                        "ever_enforced": True,
                        "active_manifest_digest": row["active_object_digest"],
                        "candidate_manifest_digest": row["active_object_digest"],
                    }
        changes["rollout_state"] = "frozen" if head["ever_enforced"] else "preparing"
        return changes, mirror
    if head["candidate_manifest_id"] is None:
        raise _unavailable()
    manifest, items, _ = await read_manifest(
        db,
        tenant_id=tenant.tenant_id,
        manifest_id=head["candidate_manifest_id"],
        expected_digest=head["candidate_manifest_digest"],
        catalog=catalog,
    )
    require(manifest["base_active_digest"] == head["active_manifest_digest"])
    proof = plan.qualification
    if type(proof) is not Qualification:
        raise BusinessException(
            409, "需要全系统字段资格", error_code="FIELD_POLICY_ACTIVATION_BLOCKED"
        )
    proof.verify(catalog, settings.RELEASE_BUILD_SHA)
    if action == "ready":
        bindings = await service._bindings(db, tenant.tenant_id, catalog)
        require(tuple(service._item_rows(bindings)) == items)
        changes.update(
            ready_manifest_digest=head["candidate_manifest_digest"],
            qualification_digest=proof.evidence_digest,
            ready_until=min(proof.expires_at, now + timedelta(minutes=15)),
        )
        return changes, None
    g_epoch = await db.scalar(select(Guard.epoch).where(Guard.guard_id == 1))
    require(
        action == "activate"
        and head["ready_manifest_digest"] == head["candidate_manifest_digest"]
        and head["qualification_digest"] == proof.evidence_digest
        and head["ready_g_epoch"] == g_epoch
        and head["ready_b_epoch"] == barrier["epoch"]
        and head["ready_until"] is not None
        and head["ready_until"] > now
    )
    for row in objects:
        object_digest = digest(
            {
                "catalog": catalog.digest,
                "object": row["object_id"],
                "items": [
                    item for item in items if item["object_id"] == row["object_id"]
                ],
            },
            4 * 1024 * 1024,
        )
        await tx.update_held(
            ResourceKey(K.FIELD_POLICY_OBJECT, tenant.tenant_id, row["object_id"]),
            {
                "mode": "enforcing",
                "ever_enforced": True,
                "object_version": row["object_version"] + 1,
                "active_object_digest": object_digest,
            },
        )
        if row["object_id"] == "system.user":
            mirror = {
                "mode": "enforcing",
                "ever_enforced": True,
                "active_manifest_digest": object_digest,
                "candidate_manifest_digest": object_digest,
            }
    changes.update(
        rollout_state="enforcing",
        ever_enforced=True,
        active_manifest_id=head["candidate_manifest_id"],
        active_manifest_digest=head["candidate_manifest_digest"],
    )
    return changes, mirror
