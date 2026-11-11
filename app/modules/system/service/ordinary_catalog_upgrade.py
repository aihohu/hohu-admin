"""Stopped, atomic expansion of exact historical preparation catalogs.

History is immutable. Current bindings receive new revisions with identical
rules, and all old previews/manifests become stale. No new grant is invented.
"""

from sqlalchemy import func, insert, select, update

from app.core.authorization_maintenance import require_maintenance
from app.core.field_policy import Catalog
from app.core.field_policy.contracts import canonical, require
from app.core.id_generator import next_id
from app.modules.system.field_policy.generic_compat import user_compat_object
from app.modules.system.field_policy.ordinary_catalog import (
    CATALOG_REVISION,
    ordinary_catalog,
    production_catalog,
)
from app.modules.system.models.field_policy import FieldPolicyBinding as Binding
from app.modules.system.models.field_policy import FieldPolicyHead as Head
from app.modules.system.models.field_policy import FieldPolicyObjectState as ObjectState
from app.modules.system.models.field_policy import FieldPolicyRevision as Revision
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard as Guard,
)
from app.modules.system.models.user_field_policy import UserFieldPolicyState as Barrier
from app.modules.system.service.generic_field_policy_migration import (
    migrate_legacy,
    register_catalog,
)
from app.modules.system.service.generic_field_policy_service import READY_FIELDS
from app.modules.system.service.generic_field_policy_service import (
    generic_field_policy_service as service,
)
from app.modules.system.service.generic_field_policy_state import read_manifest


async def prepare_catalog(db, *, fence, source_build):
    evidence = require_maintenance(fence, "migration")
    require(evidence.build_sha == source_build)
    catalog, old = production_catalog(), Catalog((user_compat_object(),), ())
    sources = {c.digest: c for c in (old, ordinary_catalog(), catalog)}
    await db.scalar(select(Guard.epoch).where(Guard.guard_id == 1).with_for_update())
    barriers = (
        (
            await db.execute(
                select(Barrier.__table__)
                .order_by(Barrier.tenant_id)
                .limit(1025)
                .with_for_update()
            )
        )
        .mappings()
        .all()
    )
    require(1 <= len(barriers) <= 1024)
    heads = (
        (await db.execute(select(Head.__table__).order_by(Head.tenant_id).limit(1025)))
        .mappings()
        .all()
    )
    if not heads:
        return await migrate_legacy(
            db,
            fence=fence,
            catalog=catalog,
            catalog_revision=CATALOG_REVISION,
            source_build=source_build,
        )
    require({r["tenant_id"] for r in heads} == {r["tenant_id"] for r in barriers})
    source_digests = {r["catalog_digest"] for r in heads}
    require(len(source_digests) == 1)
    require(
        all(
            r["catalog_digest"] in sources
            and not r["ever_enforced"]
            and r["rollout_state"] == "preparing"
            and r["active_manifest_id"] is None
            for r in heads
        )
    )
    await register_catalog(
        db, fence=fence, catalog=catalog, catalog_revision=CATALOG_REVISION
    )
    changed, mappings, decoded = [], [], 0
    for head in heads:
        require_maintenance(fence, "migration")
        tid = head["tenant_id"]
        source = sources[head["catalog_digest"]]
        await service._catalog(db, source)
        objects = await service._objects(db, tid, source)
        require(
            all(
                obj["mode"] in {"legacy", "ready"}
                and not obj["ever_enforced"]
                and obj["active_object_digest"] is None
                for obj in objects
            )
        )
        bindings = await service._bindings(db, tid, source)
        decoded += sum(len(canonical(r["rules"], 512 * 1024)) for r in bindings)
        require(decoded <= 64 * 1024 * 1024)
        if head["candidate_manifest_id"] is not None:
            await read_manifest(
                db,
                tenant_id=tid,
                manifest_id=head["candidate_manifest_id"],
                expected_digest=head["candidate_manifest_digest"],
                catalog=source,
            )
        if source.digest == catalog.digest:
            continue
        # Existing revisions/items retain their old catalog FK and content.
        for obj in objects:
            await db.execute(
                update(ObjectState)
                .where(
                    ObjectState.tenant_id == tid,
                    ObjectState.object_id == obj["object_id"],
                )
                .values(
                    catalog_digest=catalog.digest,
                    object_version=obj["object_version"] + 1,
                )
            )
        for object_id in set(catalog.objects) - set(source.objects):
            await db.execute(
                insert(ObjectState).values(
                    tenant_id=tid, object_id=object_id, catalog_digest=catalog.digest
                )
            )
        for binding in bindings:
            old_revision = (
                (
                    await db.execute(
                        select(Revision.__table__).where(
                            Revision.tenant_id == tid,
                            Revision.revision_id == binding["revision_id"],
                        )
                    )
                )
                .mappings()
                .one()
            )
            revision_no = await db.scalar(
                select(func.max(Revision.revision_no)).where(
                    Revision.tenant_id == tid,
                    Revision.role_id == binding["role_id"],
                    Revision.object_id == binding["object_id"],
                )
            )
            new_id = next_id()
            await db.execute(
                insert(Revision).values(
                    revision_id=new_id,
                    tenant_id=tid,
                    role_id=binding["role_id"],
                    object_id=binding["object_id"],
                    revision_no=revision_no + 1,
                    catalog_digest=catalog.digest,
                    rules=binding["rules"],
                    rules_digest=binding["rules_digest"],
                    source_digest=binding["rules_digest"],
                    template_revision="catalog-upgrade/" + str(binding["revision_id"]),
                    actor_user_id=old_revision["actor_user_id"],
                )
            )
            await db.execute(
                update(Binding)
                .where(
                    Binding.tenant_id == tid,
                    Binding.role_id == binding["role_id"],
                    Binding.object_id == binding["object_id"],
                )
                .values(
                    revision_id=new_id, binding_version=binding["binding_version"] + 1
                )
            )
            mappings.append(
                {"oldRevision": str(binding["revision_id"]), "newRevision": str(new_id)}
            )
        await db.execute(
            update(Head)
            .where(Head.tenant_id == tid)
            .values(
                catalog_digest=catalog.digest,
                head_version=head["head_version"] + 1,
                candidate_manifest_id=None,
                candidate_manifest_digest=None,
                **READY_FIELDS,
            )
        )
        await db.execute(
            update(Barrier)
            .where(Barrier.tenant_id == tid)
            .values(epoch=Barrier.epoch + 1)
        )
        changed.append(str(tid))
    return {
        "catalogDigest": catalog.digest,
        "sourceCatalogDigest": next(iter(source_digests)),
        "changedTenants": changed,
        "revisions": mappings,
    }
