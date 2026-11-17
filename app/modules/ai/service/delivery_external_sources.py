"""AI copies point at original FILE and transfer proofs, never copied grants."""

from hashlib import sha256

from sqlalchemy import select

from app.core.authorization_plan import ResourceKey, ResourceKind
from app.core.field_policy.contracts import canonical
from app.modules.ai.service.delivery_source_revalidation import changed
from app.modules.system.field_policy.file_sources import require_file_manifest
from app.modules.system.models.file import File
from app.modules.system.models.user_transfer import UserExportTask, UserImportBatch
from app.modules.system.service.delivery_read_context import load_delivery_context
from app.modules.system.service.file_read_service import content_snapshot
from app.modules.system.service.user_transfer_fields import user_transfer_fields


def digest(value):
    return sha256(canonical(value, 4 * 1024 * 1024)).hexdigest()


async def file_source(
    db, tx, *, tenant, version, identifier, action, read=("content",)
):
    tx.assert_session(db)
    tx.assert_barrier(tenant.tenant_id)
    row = await db.scalar(
        select(File)
        .where(
            File.tenant_id == tenant.tenant_id,
            File.file_id == identifier,
            File.owner_user_id == tenant.actor_user_id,
            File.del_flag == "0",
        )
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise changed()
    require_file_manifest(content_snapshot(row))
    context = await load_delivery_context(
        db, tx, tenant=tenant, version=version, action=action
    )
    decisions = context.decisions(
        context.record("system.file", str(identifier), context.scopes)
    )
    if any(not decisions[n].allowed for n in read):
        raise changed()
    group = {
        "kind": "file",
        "file_id": str(identifier),
        "action_id": action,
        "read": sorted(set(read)),
        "sha256": row.content_sha256,
        "manifest_digest": digest(row.source_manifest),
    }
    return group, row, decisions


async def artifact_source(
    db, tx, *, tenant, version, resource_kind, identifier, source_kind, read=()
):
    tx.assert_session(db)
    tx.assert_barrier(tenant.tenant_id)
    model, anchor = (
        (UserExportTask, "export_id")
        if resource_kind == "export"
        else (UserImportBatch, "batch_id")
    )
    row = (
        (
            await db.execute(
                select(model.__table__).where(
                    model.tenant_id == tenant.tenant_id,
                    getattr(model, anchor) == identifier,
                    model.operator_id == tenant.actor_user_id,
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        row is None
        or type(row["field_lineage"]) is not dict
        or source_kind not in row["field_lineage"]
    ):
        raise changed()
    await user_transfer_fields.download(
        db,
        tx,
        tenant=tenant,
        version=version,
        row=row,
        identifier=identifier,
        kind=source_kind,
        _after_plan=True,
        _reprove=True,
    )
    context = await load_delivery_context(
        db,
        tx,
        tenant=tenant,
        version=version,
        action="transfer."
        + ("export-list" if resource_kind == "export" else "import-list"),
    )
    decisions = context.decisions(
        context.record("transfer." + resource_kind, identifier, context.scopes)
    )
    if any(n not in decisions or not decisions[n].allowed for n in read):
        raise changed()
    return {
        "kind": "artifact",
        "resource_kind": resource_kind,
        "resource_id": identifier,
        "source_kind": source_kind,
        "lineage_digest": digest(row["field_lineage"][source_kind]),
        "read": sorted(set(read)),
    }


async def recheck_external(db, tx, *, tenant, version, group):
    if group["kind"] == "file":
        actual, _, _ = await file_source(
            db,
            tx,
            tenant=tenant,
            version=version,
            identifier=int(group["file_id"]),
            action=group["action_id"],
            read=group["read"],
        )
    else:
        actual = await artifact_source(
            db,
            tx,
            tenant=tenant,
            version=version,
            resource_kind=group["resource_kind"],
            identifier=group["resource_id"],
            source_kind=group["source_kind"],
            read=group["read"],
        )
    if actual != group:
        raise changed()


async def existing_external_keys(db, *, tenant, proof):
    """Discover before the one plan; a vanished source can only hide a body."""
    from app.modules.ai.service.delivery_source_domains import (  # noqa: PLC0415
        validate_receipt,  # noqa: PLC0415
    )

    if validate_receipt(proof) is None:
        return ()
    keys = set()
    for model, anchor, kind in (
        (File, "file_id", ResourceKind.FILE),
        (UserExportTask, "export_id", ResourceKind.EXPORT_TASK),
        (UserImportBatch, "batch_id", ResourceKind.IMPORT_BATCH),
    ):
        identifiers = set()
        for group in proof["source_domains"]:
            if group.get("kind") == "file" and model is File:
                identifiers.add(int(group["file_id"]))
            elif (
                group.get("kind") == "artifact"
                and model is not File
                and group["resource_kind"]
                == ("export" if model is UserExportTask else "import")
            ):
                identifiers.add(group["resource_id"])
        if identifiers:
            found = await db.scalars(
                select(getattr(model, anchor)).where(
                    model.tenant_id == tenant.tenant_id,
                    getattr(model, anchor).in_(identifiers),
                )
            )
            keys.update(ResourceKey(kind, tenant.tenant_id, value) for value in found)
    return tuple(keys)
