"""Freeze current post-mutation result sources separately from the approval."""

from app.modules.ai.service.delivery_external_sources import artifact_source
from app.modules.ai.service.delivery_source_capture import capture_domain
from app.modules.ai.service.delivery_source_roots import extend_source
from app.modules.ai.service.field_authority_runtime import ai_field_authority
from app.modules.system.field_policy.delivery_catalog import delivery_catalog
from app.modules.system.service.delivery_read_context import load_delivery_context


async def result_sources(db, tx, *, tenant, version, action, result):
    proof = await ai_field_authority.renew(
        db, tx, tenant=tenant, actor_user_version=version, frozen=action.field_authority
    )
    tool = action.execute_tool_name
    if tool in {"user.export", "user.import_execute", "user.import_preview"}:
        exported = tool == "user.export"
        kind = "export" if exported else "import"
        identifier = result.projection.subject_refs[0]["id"]
        names = tuple(
            f.field_id
            for f in delivery_catalog().objects["transfer." + kind].fields
            if not f.structural and f.field_id not in {"operator_name", "filename"}
        )
        group = await artifact_source(
            db,
            tx,
            tenant=tenant,
            version=version,
            resource_kind=kind,
            identifier=identifier,
            source_kind="export" if exported else "preview_source",
            read=names,
        )
        return extend_source(proof, group)
    if tool == "user.create":
        context = await load_delivery_context(
            db, tx, tenant=tenant, version=version, action="ai.tool." + tool
        )
        identifiers = tuple(
            int(ref["id"])
            for ref in result.projection.subject_refs
            if ref["type"] == "user"
        )
        proof = extend_source(
            proof,
            await capture_domain(
                db,
                context,
                "system.user",
                records=identifiers,
                readable=("user_name", "status"),
            ),
        )
    elif tool in {"role.create", "dept.create"}:
        context = await load_delivery_context(
            db, tx, tenant=tenant, version=version, action="ai.tool." + tool
        )
        role = tool.startswith("role.")
        identifiers = tuple(
            int(ref["id"])
            for ref in result.projection.subject_refs
            if ref["type"] == ("managed_role" if role else "dept")
        )
        proof = extend_source(
            proof,
            await capture_domain(
                db,
                context,
                "system.role" if role else "system.dept",
                records=identifiers,
                readable=("role_name", "role_code", "data_scope", "status")
                if role
                else ("dept_name", "parent_id", "status"),
            ),
        )
    return await ai_field_authority.renew(
        db, tx, tenant=tenant, actor_user_version=version, frozen=proof
    )
