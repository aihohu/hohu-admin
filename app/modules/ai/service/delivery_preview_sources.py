"""Sources actually used by user import matching and its displayed summary."""

from app.core.field_policy import Access
from app.modules.ai.service.delivery_external_sources import (
    artifact_source,
    file_source,
)
from app.modules.ai.service.delivery_source_capture import capture_domain
from app.modules.ai.service.delivery_source_roots import extend_source, renew_source
from app.modules.ai.service.field_authority_runtime import ai_field_authority
from app.modules.system.service.delivery_read_context import load_delivery_context


async def preview_sources(
    db, tx, *, tenant, version, source, file_id, batch_id, presence
):
    proof = await renew_source(db, tx, tenant=tenant, version=version, source=source)
    file_group, _, decisions = await file_source(
        db,
        tx,
        tenant=tenant,
        version=version,
        identifier=int(file_id),
        action="file.chat-read",
    )
    # Only the normalized, currently readable filename enters the preview.
    if decisions["original_name"].allowed:
        file_group["read"] = sorted({*file_group["read"], "original_name"})
    context = await load_delivery_context(
        db, tx, tenant=tenant, version=version, action="transfer.user-import"
    )
    match = Access("exact", "import", "match")
    fields = set().union(*(set(names) for names in presence)) if presence else set()
    specs = [
        (
            "system.user",
            ("user_name", "employee_no") if "employee_no" in fields else ("user_name",),
        )
    ]
    # Classification resolves these complete domains before reporting counts,
    # including implicit/default assignments in the existing import protocol.
    specs.append(("system.dept", ("dept_name",)))
    if "role_input" in fields:
        specs.append(("system.role", ("role_code", "role_name")))
    groups = [file_group]
    for obj, names in specs:
        groups.append(
            await capture_domain(
                db, context, obj, queries=tuple((name, match) for name in names)
            )
        )
    groups.append(
        await artifact_source(
            db,
            tx,
            tenant=tenant,
            version=version,
            resource_kind="import",
            identifier=batch_id,
            source_kind="preview_source",
            read=(
                "total_rows",
                "summary_new",
                "summary_exists",
                "summary_conflict",
                "summary_out_of_scope",
            ),
        )
    )
    return await ai_field_authority.renew(
        db,
        tx,
        tenant=tenant,
        actor_user_version=version,
        frozen=extend_source(proof, *groups),
    )
