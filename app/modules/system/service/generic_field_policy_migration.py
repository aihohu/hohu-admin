"""Offline, atomic legacy import. Never commits or grants maintenance authority."""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import insert, select, tuple_, update

from app.core.authorization_maintenance import require_maintenance
from app.core.field_policy.contracts import Catalog, canonical, primitive, require
from app.core.field_policy.documents import (
    catalog_document,
    catalog_parts,
    digest,
    load_catalog,
    rules_document,
)
from app.core.id_generator import next_id
from app.modules.system.field_policy import documents as legacy_documents
from app.modules.system.field_policy.generic_compat import (
    convert_legacy_grants,
    user_compat_object,
)
from app.modules.system.field_policy.state import valid_policy_state
from app.modules.system.models.field_policy import (
    FieldCatalogObject as CatalogObject,
)
from app.modules.system.models.field_policy import (
    FieldCatalogSnapshot as Snapshot,
)
from app.modules.system.models.field_policy import (
    FieldPolicyBinding as Binding,
)
from app.modules.system.models.field_policy import (
    FieldPolicyHead as Head,
)
from app.modules.system.models.field_policy import (
    FieldPolicyManifest as Manifest,
)
from app.modules.system.models.field_policy import (
    FieldPolicyManifestItem as Item,
)
from app.modules.system.models.field_policy import (
    FieldPolicyMigrationMap as MigrationMap,
)
from app.modules.system.models.field_policy import (
    FieldPolicyObjectState as ObjectState,
)
from app.modules.system.models.field_policy import (
    FieldPolicyRevision as Revision,
)
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyAudit as OldAudit,
)
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyBinding as OldBinding,
)
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard as Guard,
)
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyRevision as OldRevision,
)
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyState as State,
)
from app.modules.system.service.generic_field_policy_service import (
    generic_field_policy_service,
    manifest_document,
)
from app.modules.system.service.generic_field_policy_state import read_manifest


def _json(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(v) for v in value]
    return value


async def register_catalog(db, *, fence, catalog, catalog_revision):
    require_maintenance(fence, "migration")
    require(type(catalog_revision) is str and 1 <= len(catalog_revision.strip()) <= 64)
    require(all(len(obj.object_id) <= 96 for obj in catalog.objects.values()))
    doc = catalog_document(catalog)
    current = (
        (
            await db.execute(
                select(Snapshot.__table__).where(
                    Snapshot.catalog_digest == catalog.digest
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    action, adapter = catalog_parts(catalog)
    if current is not None:
        load_catalog(current["canonical_document"], expected_digest=catalog.digest)
        require(
            (
                current["action_catalog_digest"],
                current["adapter_digest"],
                current["catalog_revision"],
            )
            == (action, adapter, catalog_revision)
        )
        await generic_field_policy_service._catalog(db, catalog)
        return
    await db.execute(
        insert(Snapshot).values(
            catalog_digest=catalog.digest,
            schema_version=2,
            catalog_revision=catalog_revision,
            action_catalog_digest=action,
            adapter_digest=adapter,
            canonical_document=doc,
        )
    )
    for obj in catalog.objects.values():
        await db.execute(
            insert(CatalogObject).values(
                catalog_digest=catalog.digest,
                object_id=obj.object_id,
                domain_kind=obj.domain_kind,
                schema_digest=digest(primitive(obj)),
                adapter_version=obj.scope_adapter,
            )
        )


async def _pages(db, model, tenant_id, column, budget):
    table = model.__table__
    rows, last = [], 0
    while True:
        page = (
            (
                await db.execute(
                    select(table)
                    .where(table.c.tenant_id == tenant_id, table.c[column] > last)
                    .order_by(table.c[column])
                    .limit(16)
                )
            )
            .mappings()
            .all()
        )
        if not page:
            return rows
        for row in page:
            budget[0] += 1
            budget[1] += len(canonical(_json(dict(row)), 1024 * 1024))
            require(budget[0] <= 100000 and budget[1] <= 128 * 1024 * 1024)
            rows.append(row)
        last = page[-1][column]


async def migrate_legacy(db, *, fence, catalog, catalog_revision, source_build):
    evidence = require_maintenance(fence, "migration")
    require(
        source_build == evidence.build_sha
        and catalog.objects.get("system.user")
        == Catalog((user_compat_object(),), ()).objects["system.user"]
    )
    # One physical G, followed by all physical B rows. Stop evidence precedes this transaction.
    guard = await db.scalar(
        select(Guard.epoch).where(Guard.guard_id == 1).with_for_update()
    )
    require(type(guard) is int and guard >= 1)
    states = (
        (
            await db.execute(
                select(State.__table__)
                .order_by(State.tenant_id)
                .limit(1025)
                .with_for_update()
            )
        )
        .mappings()
        .all()
    )
    require(1 <= len(states) <= 1024)
    await register_catalog(
        db, fence=fence, catalog=catalog, catalog_revision=catalog_revision
    )
    existing = (
        (await db.execute(select(Head.__table__).order_by(Head.tenant_id).limit(1025)))
        .mappings()
        .all()
    )
    if existing:
        require({r["tenant_id"] for r in existing} == {r["tenant_id"] for r in states})
        return await _verify_replay(db, fence, catalog, source_build, states, existing)
    batch = str(uuid4())
    totals = {"tenants": 0, "revisions": 0, "audits": 0}
    budget = [0, 0]
    for state in states:
        require_maintenance(fence, "migration")
        require(
            state["catalog_revision"] == "user-fields/1"
            and state["catalog_digest"] == legacy_documents.catalog_digest()
        )
        require(
            valid_policy_state(
                state["mode"],
                state["ever_enforced"],
                state["candidate_manifest_digest"],
                state["active_manifest_digest"],
            )
        )
        tid = state["tenant_id"]
        revisions = await _pages(db, OldRevision, tid, "revision_id", budget)
        bindings = await _pages(db, OldBinding, tid, "role_id", budget)
        audits = await _pages(db, OldAudit, tid, "audit_id", budget)
        require(len(bindings) <= 1024)
        await db.execute(
            insert(Head).values(tenant_id=tid, catalog_digest=catalog.digest)
        )
        for obj in catalog.objects.values():
            if obj.domain_kind != "tenant":
                continue
            values = {
                "tenant_id": tid,
                "object_id": obj.object_id,
                "catalog_digest": catalog.digest,
            }
            if obj.object_id == "system.user" and state["ever_enforced"]:
                values.update(
                    mode=state["mode"],
                    ever_enforced=True,
                    active_object_digest=state["active_manifest_digest"],
                )
            await db.execute(insert(ObjectState).values(**values))
        mapped = {}
        for old in revisions:
            require(
                old["catalog_revision"] == "user-fields/1"
                and old["catalog_digest"] == legacy_documents.catalog_digest()
            )
            parsed = legacy_documents.read_rules(old["rules"], role_id=old["role_id"])
            require(legacy_documents.rules_digest(old["rules"]) == old["rules_digest"])
            converted = convert_legacy_grants(tid, parsed)
            document = rules_document(
                catalog,
                converted,
                tenant_id=tid,
                role_id=old["role_id"],
                object_id="system.user",
            )
            rid, rules_hash = next_id(), digest(document)
            await db.execute(
                insert(Revision).values(
                    revision_id=rid,
                    tenant_id=tid,
                    role_id=old["role_id"],
                    object_id="system.user",
                    revision_no=old["revision_no"],
                    catalog_digest=catalog.digest,
                    rules=document,
                    rules_digest=rules_hash,
                    source_digest=old["source_digest"],
                    template_revision=old["template_revision"],
                    actor_user_id=old["created_by_user_id"],
                    created_at=old["created_at"],
                )
            )
            mapped[old["revision_id"]] = (rid, rules_hash, old["role_id"])
            await _map(
                db,
                old,
                "revision",
                old["revision_id"],
                tid,
                "revision",
                rid,
                rules_hash,
                batch,
                source_build,
                catalog.digest,
            )
        items, old_items = [], []
        for binding in bindings:
            require(binding["revision_id"] in mapped)
            rid, rules_hash, role_id = mapped[binding["revision_id"]]
            require(binding["role_id"] == role_id)
            await db.execute(
                insert(Binding).values(
                    tenant_id=tid,
                    role_id=role_id,
                    object_id="system.user",
                    revision_id=rid,
                    binding_version=binding["version"],
                    archived_at=binding["archived_at"],
                )
            )
            old_revision = next(
                r for r in revisions if r["revision_id"] == binding["revision_id"]
            )
            old_items.append(
                {
                    "role_id": str(role_id),
                    "revision_id": str(binding["revision_id"]),
                    "version": binding["version"],
                    "archived": binding["archived_at"] is not None,
                    "rules_digest": old_revision["rules_digest"],
                }
            )
            if binding["archived_at"] is None:
                items.append(
                    {
                        "role_id": str(role_id),
                        "object_id": "system.user",
                        "revision_id": str(rid),
                        "rules_digest": rules_hash,
                    }
                )
        old_manifest = (
            legacy_documents.document_digest(
                {
                    "catalog_digest": legacy_documents.catalog_digest(),
                    "bindings": old_items,
                }
            )
            if old_items
            else None
        )
        require(old_manifest == state["candidate_manifest_digest"])
        mid = next_id()
        document = manifest_document(
            catalog,
            tid,
            None,
            [
                o.object_id
                for o in catalog.objects.values()
                if o.domain_kind == "tenant"
            ],
            items,
        )
        manifest_hash = digest(document, 4 * 1024 * 1024)
        action, adapter = catalog_parts(catalog)
        await db.execute(
            insert(Manifest).values(
                manifest_id=mid,
                tenant_id=tid,
                manifest_version=1,
                catalog_digest=catalog.digest,
                action_catalog_digest=action,
                adapter_digest=adapter,
                digest=manifest_hash,
                expected_item_count=len(items),
                created_by=None,
                creation_kind="migration",
            )
        )
        for item in items:
            await db.execute(
                insert(Item).values(
                    manifest_id=mid,
                    tenant_id=tid,
                    role_id=int(item["role_id"]),
                    object_id=item["object_id"],
                    revision_id=int(item["revision_id"]),
                    catalog_digest=catalog.digest,
                    rules_digest=item["rules_digest"],
                )
            )
        await db.execute(
            update(Manifest)
            .where(Manifest.manifest_id == mid, Manifest.tenant_id == tid)
            .values(status="sealed")
        )
        await db.execute(
            update(Head)
            .where(Head.tenant_id == tid)
            .values(candidate_manifest_id=mid, candidate_manifest_digest=manifest_hash)
        )
        for old in audits:
            # Old audit rows remain the authoritative actor/time facts, referenced rather than rewritten.
            await _map(
                db,
                old,
                "audit",
                old["audit_id"],
                tid,
                "legacy_audit",
                old["audit_id"],
                digest(_json(dict(old))),
                batch,
                source_build,
                catalog.digest,
            )
        await _map(
            db,
            state,
            "state",
            tid,
            tid,
            "head",
            tid,
            manifest_hash,
            batch,
            source_build,
            catalog.digest,
        )
        totals["tenants"] += 1
        totals["revisions"] += len(revisions)
        totals["audits"] += len(audits)
    require_maintenance(fence, "migration")
    return totals | {
        "batch": batch,
        "catalogDigest": catalog.digest,
        "state": "preparing",
    }


async def _verify_replay(db, fence, catalog, source_build, states, heads):
    """Exact stopped-state replay is read-only; any subsequent change rejects it."""
    budget, batches = [0, 0], set()
    totals = {"tenants": 0, "revisions": 0, "audits": 0}
    for state, head in zip(states, heads, strict=True):
        require_maintenance(fence, "migration")
        tid = state["tenant_id"]
        require(
            head["catalog_digest"] == catalog.digest
            and head["head_version"] == 1
            and head["rollout_state"] == "preparing"
            and not head["ever_enforced"]
            and head["active_manifest_id"] is None
            and head["ready_manifest_digest"] is None
        )
        revisions = await _pages(db, OldRevision, tid, "revision_id", budget)
        audits = await _pages(db, OldAudit, tid, "audit_id", budget)
        bindings = await _pages(db, OldBinding, tid, "role_id", budget)
        maps, last = {}, ("", -1)
        while True:
            page = (
                (
                    await db.execute(
                        select(MigrationMap.__table__)
                        .where(
                            MigrationMap.tenant_id == tid,
                            tuple_(MigrationMap.legacy_table, MigrationMap.legacy_id)
                            > last,
                        )
                        .order_by(MigrationMap.legacy_table, MigrationMap.legacy_id)
                        .limit(16)
                    )
                )
                .mappings()
                .all()
            )
            if not page:
                break
            for row in page:
                budget[0] += 1
                budget[1] += len(canonical(_json(dict(row)), 1024 * 1024))
                require(budget[0] <= 100000 and budget[1] <= 128 * 1024 * 1024)
                require(
                    row["source_build"] == source_build
                    and row["catalog_digest"] == catalog.digest
                )
                batches.add(row["batch_id"])
                maps[row["legacy_table"], row["legacy_id"]] = row
            last = (page[-1]["legacy_table"], page[-1]["legacy_id"])
        sources = [("revision", r["revision_id"], r) for r in revisions]
        sources += [("audit", r["audit_id"], r) for r in audits] + [
            ("state", tid, state)
        ]
        require(set(maps) == {(kind, rid) for kind, rid, _ in sources})
        for kind, rid, old in sources:
            mapping = maps[kind, rid]
            require(
                mapping["old_digest"] == digest(_json(dict(old)))
                and mapping["role_id"] == old.get("role_id")
            )
            if kind == "audit":
                require(
                    mapping["target_kind"] == "legacy_audit"
                    and mapping["target_id"] == rid
                    and mapping["new_digest"] == mapping["old_digest"]
                )
        saved = {
            r["revision_id"]: r
            for r in await _pages(db, Revision, tid, "revision_id", budget)
        }
        require(
            set(saved)
            == {maps["revision", r["revision_id"]]["target_id"] for r in revisions}
        )
        for old in revisions:
            mapping = maps["revision", old["revision_id"]]
            row = saved[mapping["target_id"]]
            rules = rules_document(
                catalog,
                convert_legacy_grants(
                    tid,
                    legacy_documents.read_rules(old["rules"], role_id=old["role_id"]),
                ),
                tenant_id=tid,
                role_id=old["role_id"],
                object_id="system.user",
            )
            require(
                mapping["target_kind"] == "revision"
                and row["rules"] == rules
                and row["rules_digest"] == mapping["new_digest"] == digest(rules)
                and row["object_id"] == "system.user"
                and row["catalog_digest"] == catalog.digest
                and row["role_id"] == old["role_id"]
                and row["revision_no"] == old["revision_no"]
                and row["actor_user_id"] == old["created_by_user_id"]
                and row["created_at"] == old["created_at"]
                and row["source_digest"] == old["source_digest"]
                and row["template_revision"] == old["template_revision"]
            )
        current = (
            (
                await db.execute(
                    select(Binding.__table__)
                    .where(Binding.tenant_id == tid)
                    .order_by(Binding.role_id, Binding.object_id)
                    .limit(1025)
                )
            )
            .mappings()
            .all()
        )
        require(len(current) == len(bindings) <= 1024)
        for old, row in zip(bindings, current, strict=True):
            require(
                row["object_id"] == "system.user"
                and row["role_id"] == old["role_id"]
                and row["revision_id"]
                == maps["revision", old["revision_id"]]["target_id"]
                and row["binding_version"] == old["version"]
                and row["archived_at"] == old["archived_at"]
            )
        objects = (
            (
                await db.execute(
                    select(ObjectState.__table__)
                    .where(ObjectState.tenant_id == tid)
                    .limit(65)
                )
            )
            .mappings()
            .all()
        )
        require(
            {r["object_id"] for r in objects}
            == {
                o.object_id
                for o in catalog.objects.values()
                if o.domain_kind == "tenant"
            }
        )
        for obj in objects:
            inherited = obj["object_id"] == "system.user" and state["ever_enforced"]
            require(
                obj["catalog_digest"] == catalog.digest
                and obj["object_version"] == 1
                and obj["mode"] == (state["mode"] if inherited else "legacy")
                and obj["ever_enforced"] == bool(inherited)
                and obj["active_object_digest"]
                == (state["active_manifest_digest"] if inherited else None)
            )
        mapping = maps["state", tid]
        require(
            mapping["target_kind"] == "head"
            and mapping["target_id"] == tid
            and mapping["new_digest"] == head["candidate_manifest_digest"]
        )
        manifest, items, _ = await read_manifest(
            db,
            tenant_id=tid,
            manifest_id=head["candidate_manifest_id"],
            expected_digest=mapping["new_digest"],
            catalog=catalog,
        )
        require(
            manifest["creation_kind"] == "migration"
            and manifest["manifest_version"] == 1
            and manifest["base_active_digest"] is None
        )
        expected = [
            {
                "role_id": str(r["role_id"]),
                "object_id": "system.user",
                "revision_id": str(r["revision_id"]),
                "rules_digest": saved[r["revision_id"]]["rules_digest"],
            }
            for r in current
            if r["archived_at"] is None
        ]
        require(items == tuple(expected))
        totals["tenants"] += 1
        totals["revisions"] += len(revisions)
        totals["audits"] += len(audits)
    require(len(batches) == 1)
    require_maintenance(fence, "migration")
    return totals | {
        "batch": next(iter(batches)),
        "catalogDigest": catalog.digest,
        "state": "preparing",
    }


async def _map(
    db,
    old,
    source,
    legacy_id,
    tenant_id,
    target,
    target_id,
    new_digest,
    batch,
    build,
    catalog_digest,
):
    await db.execute(
        insert(MigrationMap).values(
            legacy_table=source,
            legacy_id=legacy_id,
            tenant_id=tenant_id,
            role_id=old.get("role_id"),
            target_kind=target,
            target_id=target_id,
            old_digest=digest(_json(dict(old))),
            new_digest=new_digest,
            batch_id=batch,
            source_build=build,
            catalog_digest=catalog_digest,
        )
    )
