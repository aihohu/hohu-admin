"""Fixed REST import handoff; multipart/parse work never holds database locks."""

import asyncio
from uuid import uuid4

from app.core.exceptions import BusinessRuleException, UnprocessableEntityException
from app.core.file_storage import ArtifactExecution
from app.db.authorization_http import get_authorization_runtime, release_request_reads
from app.db.authorization_plan_session import _order_error
from app.db.authorization_transfer_owner import transfer_parser
from app.modules.auth.service import access_identity
from app.modules.system.constants import EmployeeNoSyncMode
from app.modules.system.service.file_policy_service import file_policy_service
from app.modules.system.service.user_import_parser import (
    ImportErrorCollection,
    import_file_has_column,
    parse_import_excel,
)


def _parse(data, mime):
    return parse_import_excel(data, mime), import_file_has_column(
        data, mime, "role_input"
    )


async def export_http(db, actor, *, tenant, body):
    identity = access_identity(actor)
    if identity.tenant != tenant:
        raise _order_error()
    payload = body.model_dump()
    await release_request_reads(db)
    result = await get_authorization_runtime().transfer.export(
        tenant=identity.tenant, actor_user_version=identity.user_version, args=payload
    )
    return result.data, result.filename


async def download_http(db, actor, *, tenant, identifier):
    identity = access_identity(actor)
    if identity.tenant != tenant:
        raise _order_error()
    await release_request_reads(db)
    return await get_authorization_runtime().transfer.download(
        tenant=identity.tenant,
        actor_user_version=identity.user_version,
        kind="export",
        identifier=identifier,
        administrative=True,
    )


async def cancel_http(db, actor, *, tenant, identifier, reason):
    identity = access_identity(actor)
    if identity.tenant != tenant:
        raise _order_error()
    await release_request_reads(db)
    return await get_authorization_runtime().transfer.cancel(
        tenant=identity.tenant,
        actor_user_version=identity.user_version,
        identifier=identifier,
        reason=reason,
    )


async def import_http(
    db, actor, *, tenant, file, reason, on_conflict, sync_mode, dry_run, preview_token
):
    identity = access_identity(actor)
    if identity.tenant != tenant:
        raise _order_error()
    async with asyncio.timeout(5):
        policy = await file_policy_service.resolve(db, "import", tenant=tenant)
    await release_request_reads(db)
    async with asyncio.timeout(5):
        data = await file.read(policy.max_bytes + 1)
    file_policy_service.validate_import(
        policy, file.filename or "users.xlsx", len(data)
    )
    if not dry_run and (not preview_token or not preview_token.strip()):
        raise UnprocessableEntityException(
            "dry_run=false 时 preview_token 必填",
            error_code="AI_IMPORT_PREVIEW_INVALID",
        )
    runtime = get_authorization_runtime()
    try:
        if dry_run:
            result = await runtime.transfer.preview(
                tenant=identity.tenant,
                actor_user_version=identity.user_version,
                file_bytes=data,
                filename=file.filename or "users.xlsx",
                mime_type=file.content_type or "",
                reason=reason,
                on_conflict=on_conflict,
            )
            return result.preview_data
        records, has_role_column = await transfer_parser.run(
            _parse, data, file.content_type or ""
        )
    except ImportErrorCollection as exc:
        wrapped = BusinessRuleException(
            f"{len(exc.errors)} 个字段错误", error_code="AI_IMPORT_FIELD_ERRORS"
        )
        wrapped.data = {
            "errors": [
                error.model_dump(mode="json", by_alias=True) for error in exc.errors
            ]
        }
        raise wrapped from exc
    result = await runtime.import_users(
        tenant=identity.tenant,
        actor_user_version=identity.user_version,
        records=records,
        file_bytes=data,
        filename=file.filename or "users.xlsx",
        reason=reason,
        preview_token=preview_token.strip(),
        on_conflict=on_conflict,
        sync_mode=EmployeeNoSyncMode(sync_mode),
        has_role_column=has_role_column,
        file_storage=runtime.transfer.storage,
        artifact_execution=ArtifactExecution(
            tenant.tenant_id, "rest-import", uuid4().hex
        ),
    )
    return result.model_dump(mode="json", by_alias=True)
